from __future__ import annotations
import base64
import hashlib
import json
from pathlib import Path
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import pytest
from aa_arena.benchmark.evaluation_server import EvaluationStore, serve
from aa_arena.benchmark.remote import (
    EvaluationClient,
    pack_strategy,
    unpack_strategy,
    RemoteMatchService,
)
from aa_arena.benchmark.experiment import ExperimentConfig
from aa_arena.benchmark.matches import MatchInfrastructureError
from aa_arena.resources import ARENA_GAMES, _rating_rows, REPOSITORY_ROOT


class FakeMatches:
    calls = []

    def __init__(self, game, run_root, **kwargs):
        self.game = game
        self.run_root = run_root

    def preflight_candidate(self, path):
        assert (path / "main.py").exists()

    def small_match(self, path, ids, sid, replays, **kwargs):
        self.calls.append((self.game, sid, "small"))
        if kwargs.get("feedback") == "binary":
            return {"kind": "small", "opponents": ids, "binary_outcomes": [True]}
        root = replays / sid / ids[0]
        root.mkdir(parents=True, exist_ok=True)
        (root / "P0.json").write_text('{"rounds":[{"round":1}]}')
        (root / "P0.md").write_text("Public game observations")
        return {
            "kind": "small",
            "opponents": ids,
            "wins": 1,
            "seats": [
                {
                    "opponent_id": ids[0],
                    "diagnostic": "PRIVATE SOURCE SHOULD NEVER BE SENT",
                    "replay_path": str(root / "P0.json"),
                    "narration_path": str(root / "P0.md"),
                }
            ],
        }

    def large_match(self, path, sid):
        self.calls.append((self.game, sid, "large"))
        return {
            "kind": "large",
            "elo": 1000.0,
            "rank": 20,
            "games": len(_rating_rows(self.game, REPOSITORY_ROOT)) * 2,
        }


class FixtureStore(EvaluationStore):
    def inventory(self, game):
        return [
            {k: r.get(k) for k in ("opponent_id", "rank", "elo", "track")}
            for r in _rating_rows(game, REPOSITORY_ROOT)
        ]


@pytest.fixture
def api(tmp_path):
    users = {
        x: {"token_sha256": hashlib.sha256(x.encode()).hexdigest(), "max_runs": 100}
        for x in ("owner-a", "owner-b")
    }
    store = FixtureStore(
        tmp_path / "server", REPOSITORY_ROOT, users, match_factory=FakeMatches, workers=1, jobs=2
    )
    server = serve(store, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    client = EvaluationClient(url, "owner-a")
    yield store, client, url
    server.shutdown()
    server.server_close()
    store.close()
    thread.join()


def open_run(client, game="pacman", run="run", small=2, large=1, config=None):
    return client.request(
        "POST",
        "/v1/runs",
        {
            "run_id": run,
            "game": game,
            "small_total": small,
            "large_total": large,
            "seed": 20260831,
            "experiment": (config or ExperimentConfig()).as_dict(),
        },
    )


def payload(opponent, *, sid="job", kind="small", feedback="detailed"):
    return {
        "submission_id": sid,
        "kind": kind,
        "files": [
            {
                "path": "main.py",
                "data": base64.b64encode(b"print(1)\n").decode(),
                "executable": False,
            }
        ],
        "opponent_ids": [opponent] if kind == "small" else [],
        "feedback": feedback,
        "seed": 20260831,
        "allow_repeats": False,
    }


def wait(client, run, sid):
    for _ in range(500):
        r = client.request("GET", f"/v1/runs/{run}/jobs/{sid}")
        if r["status"] not in {"queued", "running"}:
            return r
        time.sleep(0.01)
    raise AssertionError("job timed out")


@pytest.mark.parametrize("game", ARENA_GAMES)
def test_all_games_small_large_roundtrip(api, game):
    store, c, url = api
    run = open_run(c, game)
    opponent = run["opponents"][0]["opponent_id"]
    p = payload(opponent)
    c.request("POST", "/v1/runs/run/jobs", p)
    r = wait(c, "run", "job")
    assert r["status"] == "complete"
    assert "PRIVATE SOURCE" not in json.dumps(r)
    assert all(not a["path"].startswith("/") for a in r["artifacts"])
    assert len(r["artifacts"]) == 2
    c.request("POST", "/v1/runs/run/jobs", payload(opponent, sid="large", kind="large"))
    r = wait(c, "run", "large")
    assert r["result"]["games"] == len(run["opponents"]) * 2
    assert r["receipt"]["small_used"] == 1 and r["receipt"]["large_used"] == 1


def test_idempotency_conflict_and_budget(api):
    store, c, url = api
    r = open_run(c, small=1)
    p = payload(r["opponents"][0]["opponent_id"])
    c.request("POST", "/v1/runs/run/jobs", p)
    wait(c, "run", "job")
    before = len(FakeMatches.calls)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: c.request("POST", "/v1/runs/run/jobs", p), range(8)))
    assert len(FakeMatches.calls) == before
    changed = dict(p, seed=42)
    with pytest.raises(MatchInfrastructureError):
        c.request("POST", "/v1/runs/run/jobs", changed)
    with pytest.raises(MatchInfrastructureError):
        c.request("POST", "/v1/runs/run/jobs", dict(p, submission_id="over-budget"))
    with store.db() as db:
        assert db.execute("select small_used from runs").fetchone()[0] == 1


def test_auth_and_other_user_cannot_read_job(api):
    store, c, url = api
    r = open_run(c)
    p = payload(r["opponents"][0]["opponent_id"])
    c.request("POST", "/v1/runs/run/jobs", p)
    for client, path in [
        (EvaluationClient(url, "bad"), "/v1/health"),
        (EvaluationClient(url, "owner-b"), "/v1/runs/run/jobs/job"),
        (c, "/v1/source/anything"),
    ]:
        with pytest.raises(MatchInfrastructureError):
            client.request("GET", path)


def test_binary_no_replays(api):
    store, c, url = api
    r = open_run(c, config=ExperimentConfig(feedback="binary"))
    c.request(
        "POST", "/v1/runs/run/jobs", payload(r["opponents"][0]["opponent_id"], feedback="binary")
    )
    r = wait(c, "run", "job")
    assert r["status"] == "complete" and r["artifacts"] == []


def test_continuation_adds_256_32_once(api):
    store, c, url = api
    open_run(c, small=128, large=16)
    with store.db() as db:
        db.execute("update runs set small_used=100,large_used=16")
    open_run(c, small=384, large=48)
    open_run(c, small=384, large=48)
    with store.db() as db:
        r = db.execute("select * from runs").fetchone()
        assert (
            r["small_total"] - r["small_used"] == 256
            and r["large_total"] - r["large_used"] == 32
            and r["extended"] == 1
        )
    with pytest.raises(MatchInfrastructureError):
        open_run(c, run="new", small=384, large=48)


@pytest.mark.parametrize(
    "name", ["../secret", "/tmp/secret", "a/../../secret", "a\\secret", "a//b", "a/./b"]
)
def test_upload_rejects_traversal(tmp_path, name):
    with pytest.raises(ValueError):
        unpack_strategy([{"path": name, "data": "YQ=="}], tmp_path / "strategy")
    assert not (tmp_path / "strategy").exists()


def test_upload_symlinks_and_duplicates(tmp_path):
    p = tmp_path / "src"
    p.mkdir()
    (p / "main.py").write_text("pass")
    (p / "link").symlink_to("main.py")
    with pytest.raises(ValueError):
        pack_strategy(p)
    with pytest.raises(ValueError):
        unpack_strategy([{"path": "main.py", "data": "YQ=="}] * 2, tmp_path / "dst")


def test_client_requires_endpoint_and_tls(monkeypatch):
    monkeypatch.delenv("AA_ARENA_EVAL_URL", raising=False)
    monkeypatch.delenv("AA_ARENA_EVAL_TOKEN", raising=False)
    with pytest.raises(ValueError):
        EvaluationClient()
    with pytest.raises(ValueError):
        EvaluationClient("http://public.example", "token")
    with pytest.raises(ValueError):
        EvaluationClient("https://user:pass@example.com", "token")


def test_remote_client_restores_replay_and_preserves_pool(api, tmp_path, monkeypatch):
    store, c, url = api
    monkeypatch.setenv("AA_ARENA_EVAL_URL", url)
    monkeypatch.setenv("AA_ARENA_EVAL_TOKEN", "owner-a")
    # Plugin registry is independent of transport, and not needed for this fixture.
    from types import SimpleNamespace

    monkeypatch.setattr(
        "aa_arena.benchmark.remote.get_plugin", lambda *a: SimpleNamespace(roles=("P0", "P1"))
    )
    m = RemoteMatchService(
        "pacman",
        tmp_path / "client",
        run_id="client",
        small_budget=2,
        large_budget=1,
        experiment=ExperimentConfig().as_dict(),
    )
    p = tmp_path / "strategy"
    p.mkdir()
    (p / "main.py").write_text("print(1)")
    result = m.small_match(p, [m.opponents[0].opponent_id], "small", tmp_path / "replays")
    seat = result["seats"][0]
    assert Path(seat["replay_path"]).is_file() and Path(seat["narration_path"]).is_file()
    assert result["evaluation_receipt"]["pool_sha256"] == m.pool_sha256


def test_single_server_owns_state(api):
    store, c, url = api
    with pytest.raises(BlockingIOError):
        FixtureStore(store.root, REPOSITORY_ROOT, store.users, match_factory=FakeMatches)


def test_failed_job_recovers_without_second_charge(api, monkeypatch):
    store, c, url = api
    run = open_run(c, small=1)
    p = payload(run["opponents"][0]["opponent_id"])
    original = FakeMatches.small_match

    def broken(*a, **k):
        raise RuntimeError("private failure with source paths")

    monkeypatch.setattr(FakeMatches, "small_match", broken)
    c.request("POST", "/v1/runs/run/jobs", p)
    first = wait(c, "run", "job")
    assert first["submission_id"] == "job" and first["status"] == "failed"
    assert first["error"]["code"] == "evaluation_failed"
    assert first["error"]["phase"] == "evaluation"
    assert first["error"]["retryable"] is True
    monkeypatch.setattr(FakeMatches, "small_match", original)
    c.request("POST", "/v1/runs/run/jobs", p)
    second = wait(c, "run", "job")
    assert second["status"] == "complete" and second["receipt"]["small_used"] == 1
    with store.db() as db:
        assert db.execute("select attempts from jobs").fetchone()[0] == 2


def test_server_restart_recovers_accepted_job(api):
    store, c, url = api
    r = open_run(c, small=1)
    p = payload(r["opponents"][0]["opponent_id"])
    c.request("POST", "/v1/runs/run/jobs", p)
    wait(c, "run", "job")
    store.close()
    with store.db() as db:
        db.execute("update jobs set status='running',result=NULL")
    recovered = FixtureStore(
        store.root, REPOSITORY_ROOT, store.users, match_factory=FakeMatches, workers=1, jobs=1
    )
    try:
        for _ in range(500):
            result = recovered.status("owner-a", "run", "job")
            if result["status"] == "complete":
                break
            time.sleep(0.01)
        assert result["status"] == "complete" and result["receipt"]["small_used"] == 1
    finally:
        recovered.close()


def test_catalog_download_has_replays_only(api, tmp_path, monkeypatch):
    from aa_arena.benchmark.remote import download_catalog

    store, c, url = api
    root = tmp_path / "catalogs"
    game = root / "pacman"
    (game / "replays").mkdir(parents=True)
    body = b'{"rounds":[]}'
    (game / "replays/one.json").write_bytes(body)
    data = {
        "protocol": "offpolicy-dense-v1",
        "game": "pacman",
        "trajectories": [
            {
                "trajectory_id": "one",
                "replay_file": "replays/one.json",
                "replay_sha256": hashlib.sha256(body).hexdigest(),
                "rank_a": 1,
                "rank_b": 2,
                "opponent_a_id": "a",
                "opponent_b_id": "b",
            }
        ],
    }
    (game / "manifest.json").write_text(json.dumps(data))
    store.catalog_root = root
    monkeypatch.setenv("AA_ARENA_EVAL_URL", url)
    monkeypatch.setenv("AA_ARENA_EVAL_TOKEN", "owner-a")
    path = download_catalog("pacman", tmp_path / "download")
    assert json.loads(path.read_text()) == data
    assert (path.parent / "replays/one.json").read_bytes() == body
    with pytest.raises(MatchInfrastructureError):
        c.request("GET", "/v1/catalogs/pacman/files/../source")


def test_malformed_upload_does_not_reserve_budget(api):
    store, c, url = api
    r = open_run(c, small=1)
    p = payload(r["opponents"][0]["opponent_id"])
    p["files"][0]["path"] = "../outside"
    with pytest.raises(MatchInfrastructureError):
        c.request("POST", "/v1/runs/run/jobs", p)
    with store.db() as db:
        assert db.execute("select small_used from runs").fetchone()[0] == 0
    p["files"][0]["path"] = "main.py"
    c.request("POST", "/v1/runs/run/jobs", p)
    assert wait(c, "run", "job")["status"] == "complete"


def test_preflight_error_is_public_safe_and_retry_limit_is_truthful(api, monkeypatch):
    store, c, _ = api
    run = open_run(c, small=1)
    p = payload(run['opponents'][0]['opponent_id'])
    def fail(*args):
        raise ValueError('PRIVATE source and /operator/secret/path')
    monkeypatch.setattr(FakeMatches, 'preflight_candidate', fail)
    for attempt in range(3):
        c.request('POST', '/v1/runs/run/jobs', p)
        result = wait(c, 'run', 'job')
        assert result['status'] == 'failed'
        assert result['error']['code'] == 'candidate_preflight_failed'
        assert result['error']['retryable'] is (attempt < 2)
        assert 'PRIVATE' not in json.dumps(result)
        assert '/operator/' not in json.dumps(result)
    with store.db() as db:
        assert db.execute('select small_used from runs').fetchone()[0] == 1


def test_policy_upload_cli(api, tmp_path, monkeypatch):
    import importlib.util
    import sys
    _, c, url = api
    run = open_run(c, run='cli', small=128, large=16)
    strategy = tmp_path/'policy'
    strategy.mkdir()
    (strategy/'main.py').write_text('print(1)')
    output = tmp_path/'result.json'
    script = Path(__file__).resolve().parents[1]/'scripts/evaluate_policy.py'
    spec = importlib.util.spec_from_file_location('policy_cli', script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv('AA_ARENA_EVAL_URL', url)
    monkeypatch.setenv('AA_ARENA_EVAL_TOKEN', 'owner-a')
    monkeypatch.setattr(sys, 'argv', [str(script), '--game', 'pacman', '--kind', 'small',
        '--strategy', str(strategy), '--run-id', 'cli', '--submission-id', 'cli-job',
        '--opponents', run['opponents'][0]['opponent_id'], '--output', str(output), '--seed', '20260831'])
    module.main()
    assert json.loads(output.read_text())['status'] == 'complete'


def test_corrected_policy_can_follow_failed_submission(api, monkeypatch):
    store, client, _ = api
    run = open_run(client, small=2)
    bad = payload(run["opponents"][0]["opponent_id"])
    original = FakeMatches.preflight_candidate
    def fail(*args):
        raise ValueError("invalid candidate")
    monkeypatch.setattr(FakeMatches, "preflight_candidate", fail)
    client.request("POST", "/v1/runs/run/jobs", bad)
    assert wait(client, "run", "job")["status"] == "failed"
    monkeypatch.setattr(FakeMatches, "preflight_candidate", original)
    corrected = dict(bad, submission_id="corrected")
    client.request("POST", "/v1/runs/run/jobs", corrected)
    result = wait(client, "run", "corrected")
    assert result["status"] == "complete"
    assert result["receipt"]["small_used"] == 2
    assert client.request("GET", "/v1/runs/run/jobs/job")["status"] == "failed"

def test_large_sweeps_leave_an_interactive_job_slot(api, monkeypatch):
    store, client, url = api
    gate=threading.Event()
    entered=threading.Event()
    original=FakeMatches.large_match
    def blocked(self,path,sid):
        entered.set()
        assert gate.wait(15)
        return original(self,path,sid)
    monkeypatch.setattr(FakeMatches,'large_match',blocked)
    try:
        for name in ('sweep-a','sweep-b','interactive'):
            open_run(client,run=name)
        def payload(sid,kind):
            return {'submission_id':sid,'kind':kind,'files':[{'path':'main.py','data':base64.b64encode(b'pass').decode(),'executable':False}],'opponent_ids':['pacman__p43__ai895'] if kind=='small' else [],'feedback':'detailed','seed':20260831,'allow_repeats':False}
        client.request('POST','/v1/runs/sweep-a/jobs',payload('large-a','large'))
        assert entered.wait(5)
        client.request('POST','/v1/runs/sweep-b/jobs',payload('large-b','large'))
        client.request('POST','/v1/runs/interactive/jobs',payload('small-a','small'))
        deadline=time.time()+5
        while time.time()<deadline:
            status=client.request('GET','/v1/runs/interactive/jobs/small-a')
            if status['status']=='complete':break
            time.sleep(.1)
        assert status['status']=='complete'
        assert client.request('GET','/v1/runs/sweep-b/jobs/large-b')['status']=='queued'
    finally:
        gate.set()

def test_transient_retry_preserves_payload_and_auth(monkeypatch):
    import io
    import urllib.error
    client=EvaluationClient('https://eval.example','synthetic-token')
    requests=[]
    def send(request,timeout):
        requests.append(request)
        if len(requests)<3:
            raise urllib.error.HTTPError(request.full_url,502,'gateway',{},io.BytesIO(b'private error'))
        return io.BytesIO(b'{"status":"queued"}')
    monkeypatch.setattr(client.opener,'open',send)
    monkeypatch.setattr('aa_arena.benchmark.remote.time.sleep',lambda _:None)
    assert client.request('POST','/v1/runs/run/jobs',{'submission_id':'immutable-id'})=={'status':'queued'}
    assert len({r.data for r in requests})==1
    assert all(r.get_header('Authorization')=='Bearer synthetic-token' for r in requests)

@pytest.mark.parametrize('failed_kind,allowed',[('small',True),('large',False)])
def test_continuation_after_terminal_failure(api,failed_kind,allowed):
    store,c,_=api
    open_run(c,small=128,large=16)
    with store.db() as db:
        db.execute('UPDATE runs SET small_used=128,large_used=16')
        db.execute("INSERT INTO jobs(owner,run,id,digest,payload,status,attempts) VALUES(?,?,?,?,?,'failed',1)",('owner-a','run','failed','hash',json.dumps({'kind':failed_kind})))
    if allowed:
        open_run(c,small=384,large=48)
    else:
        with pytest.raises(MatchInfrastructureError):open_run(c,small=384,large=48)

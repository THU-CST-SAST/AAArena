from pathlib import Path
from aa_arena.legacy.ai9 import Ai9GameConfig, Ai9Backend, Ai9PreparedPlayer, run_ai9_match
from aa_arena.core.contract import EvaluationStatus
import pytest


@pytest.mark.parametrize('output_bytes', [0, 100000])
def test_match_waits_for_backend_replay_finalization(tmp_path, output_bytes, monkeypatch):
    # This fixture is a host Python script, not an ELF judge from the runtime.
    monkeypatch.setattr('aa_arena.core.reference_runtime.backend_command', lambda argv: argv)
    class ReadyPlayer:
        port = 12345
        def __init__(self, loader, library, directory, index, **kwargs):
            (directory / 'ready').touch()
        def close(self):
            pass
    monkeypatch.setattr('aa_arena.legacy.ai9_isolation.IsolatedAI9Player', ReadyPlayer)
    loader = tmp_path / 'loader'
    loader.write_text('#!/usr/bin/env python3\nimport time\nfrom pathlib import Path\nprint(12345, flush=True)\nprint("x" * '+str(output_bytes)+', flush=True)\nPath("ready").touch()\ntime.sleep(20)\n')
    loader.chmod(0o755)
    logic = tmp_path / 'logic'
    logic.write_text('#!/usr/bin/env python3\nfrom pathlib import Path\nimport time\ndeadline=time.monotonic()+2\nwhile not Path("ready").exists() and time.monotonic()<deadline: time.sleep(.01)\nassert Path("ready").exists()\np=Path("replay.txt")\np.write_text("Round:0\\nwinner:0\\n")\ntime.sleep(.4)\np.write_text("")\ntime.sleep(.2)\np.write_text("Round:1\\nfinalized\\nwinner:1\\n")\n')
    logic.chmod(0o755)
    library = tmp_path / 'player.so'
    library.write_bytes(b'test fixture')
    config = Ai9GameConfig('lota', tmp_path, (), (), ('P0', 'P1'), 2)
    result = run_ai9_match(config, Ai9Backend(logic, loader),
        [Ai9PreparedPlayer('a', library), Ai9PreparedPlayer('b', library)],
        build_root=tmp_path/'build', artifact_root=tmp_path/'artifacts', seed=1, timeout_s=5)
    assert result.status is EvaluationStatus.COMPLETE
    assert result.winner == 'P1'
    assert 'finalized' in Path(result.replay_path).read_text()


def test_backend_receives_each_match_seed_without_host_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr('aa_arena.core.reference_runtime.backend_command', lambda argv: argv)
    class ReadyPlayer:
        port = 12345
        def __init__(self, *args, **kwargs):
            pass
        def close(self):
            pass
    monkeypatch.setattr('aa_arena.legacy.ai9_isolation.IsolatedAI9Player', ReadyPlayer)
    monkeypatch.setenv('AA_ARENA_GAME_SEED', '999')
    monkeypatch.setenv('OPENAI_API_KEY', 'host-only-test-secret')
    logic = tmp_path / 'logic'
    logic.write_text(
        '#!/usr/bin/env python3\nimport os\nfrom pathlib import Path\n'
        'assert "OPENAI_API_KEY" not in os.environ\n'
        'seed = os.environ["AA_ARENA_GAME_SEED"]\n'
        'Path("replay.txt").write_text("Round:1\\nseed:" + seed + "\\nwinner:0\\n")\n'
    )
    logic.chmod(0o755)
    library = tmp_path / 'player.so'
    library.write_bytes(b'test fixture')
    config = Ai9GameConfig('lota', tmp_path, (), (), ('P0', 'P1'), 2)
    for seed in [1234567, 7654321]:
        result = run_ai9_match(config, Ai9Backend(logic, logic),
            [Ai9PreparedPlayer('a', library), Ai9PreparedPlayer('b', library)],
            build_root=tmp_path/'build', artifact_root=tmp_path/'artifacts',
            seed=seed, timeout_s=5)
        assert result.status is EvaluationStatus.COMPLETE
        assert f'seed:{seed}\n' in Path(result.replay_path).read_text()

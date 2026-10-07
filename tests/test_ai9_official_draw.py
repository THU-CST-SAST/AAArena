from types import SimpleNamespace
from aa_arena.legacy.ai9 import Ai9GameConfig, _parse_result
from aa_arena.elo.runner import _evaluate_case
from aa_arena.benchmark.matches import MatchService, Opponent


def official_draw(tmp_path):
    (tmp_path/'replay.txt').write_text('Round:0\nwinner:-2\nAI STATUS:AI(ID:0)Failed,status:SIGNALED msg:;AI(ID:1)Failed,status:SEGFAULT msg:;\n')
    return _parse_result(Ai9GameConfig('lota', tmp_path, (), (), ('P0', 'P1'), 3), tmp_path, 0)


def test_elo_accepts_explicit_backend_draw_with_player_errors(tmp_path, monkeypatch):
    result = official_draw(tmp_path)
    monkeypatch.setattr('aa_arena.elo.runner._configured_evaluator',
        lambda *args: SimpleNamespace(evaluate=lambda *args: result))
    case = SimpleNamespace(case_id='case', roles=('P0', 'P1'), seed=7,
        player_ids_by_role=('a','b'),
        player_a=SimpleNamespace(player_id='a',package_root=tmp_path),
        player_b=SimpleNamespace(player_id='b',package_root=tmp_path))
    row = _evaluate_case('lota', case, tmp_path/'build', tmp_path/'artifacts')
    assert row.status == 'game_error'
    assert row.score_a == .5


def test_benchmark_accepts_explicit_backend_draw_with_player_errors(tmp_path, monkeypatch):
    result = official_draw(tmp_path)
    monkeypatch.setattr('aa_arena.benchmark.matches._configured_evaluator',
        lambda *args: SimpleNamespace(evaluate=lambda *args: result))
    service = MatchService.__new__(MatchService)
    service.game='lota';service.roles=('P0','P1');service.seed=7
    service.hidden_root=tmp_path;service.build_root=tmp_path/'build'
    service.repository=tmp_path;service.infrastructure_retries=0
    row = service._evaluate_seat(tmp_path, Opponent('b',1500,1,tmp_path),('P0',),0,'case')
    assert row.status == 'game_error'
    assert row.outcome == 'draw'
    assert row.score == .5

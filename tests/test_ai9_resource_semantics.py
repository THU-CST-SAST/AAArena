from types import SimpleNamespace

import pytest

from aa_arena import resources
from aa_arena.core.contract import EvaluateResult, EvaluationStatus


@pytest.mark.parametrize('game', ['lota', 'monecraft', 'pacman', 'dorado'])
def test_live_certification_rejects_zero_round_success(tmp_path, monkeypatch, game):
    monkeypatch.setattr(resources, '_certify_player_visibility', lambda _: None)
    monkeypatch.setattr(resources, 'evaluate', lambda *a, **kw: EvaluateResult(
        status=EvaluationStatus.COMPLETE, winner='P0', scores={'P0': 1., 'P1': 0.}, rounds=0))
    with pytest.raises(RuntimeError, match='no valid rounds'):
        resources.certify(game, output_root=tmp_path, live=True)

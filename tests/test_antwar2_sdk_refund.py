"""Public SDK accepts the optional tower used by health-scaled refunds."""
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def sdk():
    root = Path(__file__).resolve().parents[1] / 'games/antwar2/public_sdk'
    saved = {key: value for key, value in sys.modules.items() if key == 'SDK' or key.startswith('SDK.')}
    for key in saved:
        del sys.modules[key]
    sys.path.insert(0, str(root))
    try:
        state = importlib.import_module('SDK.backend.state')
        constants = importlib.import_module('SDK.utils.constants')
        yield state.PythonBackendState.initial(seed=7), constants
    finally:
        sys.path.remove(str(root))
        for key in list(sys.modules):
            if key == 'SDK' or key.startswith('SDK.'):
                del sys.modules[key]
        sys.modules.update(saved)


@pytest.mark.parametrize('name,cost_name', [('HEAVY', 'LEVEL2_TOWER_UPGRADE_COST'), ('HEAVY_PLUS', 'LEVEL3_TOWER_UPGRADE_COST')])
@pytest.mark.parametrize('hp', [100, 50, 0, -10])
def test_downgrade_refund_supports_damaged_towers(sdk, name, cost_name, hp):
    state, constants = sdk
    tower_type = getattr(constants.TowerType, name)
    refund = getattr(constants, cost_name) * constants.TOWER_DOWNGRADE_REFUND_RATIO
    assert state.downgrade_tower_income(tower_type) == int(refund)
    tower = SimpleNamespace(hp=hp, max_hp=100)
    assert state.downgrade_tower_income(tower_type, tower) == int(refund * max(hp, 0) / 100)

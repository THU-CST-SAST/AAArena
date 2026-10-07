import importlib.util,sys,types
from pathlib import Path
import pytest

backend=Path(__file__).resolve().parents[1]/'games/miracle/backend/StateSystem'
package=types.ModuleType('miracle_capacity_regression');package.__path__=[str(backend)];sys.modules[package.__name__]=package
spec=importlib.util.spec_from_file_location(package.__name__+'.CreatureCapacity',backend/'CreatureCapacity.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

@pytest.mark.parametrize('name',list(module.UNIT_DATA))
def test_catalogue_capacity_progression_and_ceiling_preserve_occupied_slots(name):
    c=module.CreatureCapacity(name);levels=module.UNIT_DATA[name]['duplicate'];c.summon()
    for tier,capacity in enumerate(levels[1:],start=1):
        c.duplicate_level_up()
        assert (c.duplicate_level,c.duplicate,c.available_count)==(tier,capacity,capacity-1)
    old=(c.duplicate_level,c.duplicate,c.available_count)
    for _ in range(4):c.duplicate_level_up()
    assert (c.duplicate_level,c.duplicate,c.available_count)==old

def test_inferno_remains_one_slot_through_both_scheduled_upgrades():
    c=module.CreatureCapacity('Inferno');c.summon();c.new_cool_down(1)
    cooldown=c.cool_down_list.copy()
    for _ in (51,76):c.duplicate_level_up()
    assert c.duplicate==1 and c.available_count==0 and c.cool_down_list==cooldown
    for _ in range(cooldown[0]):c.cool_down()
    assert c.available_count==1

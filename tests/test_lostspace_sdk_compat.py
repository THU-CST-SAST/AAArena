"""Dispatcher behavior, immutable policy files and safe cache publication."""
import concurrent.futures
import hashlib
import json
from pathlib import Path

import pytest
from aa_arena.core import lostspace_sdk as sdk

SOURCE = '''from types import SimpleNamespace
STATUS = SimpleNamespace(ALIVE=SimpleNamespace(value=0))
class Client:
    def __init__(self, status):
        self.__player = SimpleNamespace(status=status)
        self.actions = []
    def __start_turn(self):
        if self.__player.status != STATUS.ALIVE.value:
            return
        self.play()
        self.actions.append("finish")
    def play(self):
        self.actions.append("unchanged-policy")
'''

@pytest.mark.parametrize('status,acts', [(0, ['unchanged-policy', 'finish']), (4, ['unchanged-policy', 'finish']), (1, []), (2, []), (3, []), (5, [])])
def test_dispatch_only_runs_live_or_waiting_turns(status, acts):
    scope = {}
    exec(sdk._repair_dispatch(SOURCE), scope)
    client = scope['Client'](status)
    client._Client__start_turn()
    assert client.actions == acts


def test_unknown_source_is_unchanged(tmp_path):
    (tmp_path / 'main.py').write_text(SOURCE)
    assert sdk.prepare_lostspace_sdk(tmp_path, tmp_path / 'cache') == tmp_path
    assert not (tmp_path / 'cache').exists()


def test_pinned_runtime_preserves_sources_and_rejects_cache_tampering(tmp_path, monkeypatch):
    source = tmp_path / 'source'; source.mkdir()
    (source / 'main.py').write_text(SOURCE)
    (source / 'strategy.txt').write_text('unchanged policy artifact')
    monkeypatch.setattr(sdk, '_MAIN_SHA', hashlib.sha256(SOURCE.encode()).hexdigest())
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        paths = list(pool.map(lambda _: sdk.prepare_lostspace_sdk(source, tmp_path / 'cache'), range(8)))
    assert len(set(paths)) == 1
    assert (source / 'main.py').read_text() == SOURCE
    manifest = json.loads((paths[0] / '.aa-sdk-runtime.json').read_text())
    assert manifest['changed_function'] == '__start_turn'
    assert (paths[0] / 'strategy.txt').read_text() == 'unchanged policy artifact'
    (paths[0] / 'strategy.txt').write_text('tampered')
    with pytest.raises(OSError, match='integrity'):
        sdk.prepare_lostspace_sdk(source, tmp_path / 'cache')


def test_material_reply_contains_only_current_players_inventory():
    import ast
    import tarfile
    from types import SimpleNamespace
    from enum import IntEnum
    archive = Path(__file__).resolve().parents[1] / 'assets/lostspace.tar.gz'
    def method(file, name):
        with tarfile.open(archive) as tar:
            source = tar.extractfile('games/lostspace/backend/src/' + file).read().decode()
        node = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.FunctionDef) and n.name == name)
        scope = {'PlayerStatus': IntEnum('PlayerStatus', {'Alive': 0, 'WaitForEsacape': 4}), 'PropList': ['Materials', 'KeyMachine'], 'ToolList': ['Kit']}
        exec(compile(ast.Module(body=[node], type_ignores=[]), file, 'exec'), scope)
        return scope[name]
    counts = {'LandMine': 2, 'Sticky': 1}
    bag = SimpleNamespace(has=counts, tools={'Kit': SimpleNamespace(num=lambda: 2), 'Transport': SimpleNamespace(num=lambda: 0)})
    bag.nums_dict = lambda: method('toolbag.py', 'nums_dict')(bag)
    class IO:
        respond_action = method('communicate.py', 'respond_action')
        material_response = method('communicate.py', 'material_response')
        def respond(self, state, player, kind, payload):
            self.receipt = (state, player, kind, payload)
    io = IO()
    controller = SimpleNamespace(round=3, curid=2, curplayer=SimpleNamespace(status=0, tools=bag), io=io, map=SimpleNamespace(interact=lambda *args: True))
    solve = method('GameController.py', 'solve')
    assert solve(controller, {'type': 'action', 'action': ['interact', 'Materials', 'Kit']}) is False
    assert io.receipt == (3, 2, 'action', {'tools': {'LandMine': 2, 'Sticky': 1, 'Kit': 2, 'Transport': 0}, 'success': True})
    assert counts == {'LandMine': 2, 'Sticky': 1}
    controller.map.interact = lambda *args: False
    solve(controller, {'type': 'action', 'action': ['interact', 'Materials', 'Kit']})
    assert io.receipt == (3, 2, 'action', {'success': False})
    controller.map.interact = lambda *args: True
    solve(controller, {'type': 'action', 'action': ['interact', 'KeyMachine']})
    assert io.receipt == (3, 2, 'action', {'success': True})

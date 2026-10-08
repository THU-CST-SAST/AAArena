"""LostSpace requests replies only during turns that can accept actions."""
import importlib
import json
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def backend(monkeypatch):
    name = '_lostspace_listen_test'
    package = types.ModuleType(name)
    package.__path__ = [str(Path(__file__).resolve().parents[1] / 'games/lostspace/backend/src')]
    monkeypatch.setitem(sys.modules, name, package)
    module = importlib.import_module(name + '.communicate')
    messages = []
    monkeypatch.setattr(module, 'send_message_goal', lambda body, target: messages.append((json.loads(body), target)))
    yield module, messages
    for key in list(sys.modules):
        if key.startswith(name + '.'):
            sys.modules.pop(key)


@pytest.mark.parametrize('status,requires_reply', [
    ('Alive', True), ('WaitForEsacape', True), ('Escaped', False),
    ('Died', False), ('Skip', False), ('Error', False),
])
def test_turn_notifications_preserve_contents_and_only_wait_for_actions(backend, status, requires_reply):
    module, messages = backend
    game = types.SimpleNamespace(
        players=[types.SimpleNamespace(status=getattr(module.PlayerStatus, status))],
        player_property=[module.PlayDevice.MediaPlayer.value],
    )
    io = module.Communicate(game)
    io.small_round = 19
    notice = {'type': 'roundbegin', 'state': 4, 'status': getattr(module.PlayerStatus, status).value}
    io.__send__([0], [0], [notice])
    wire, target = messages.pop()
    assert target == -1
    assert wire == {'state': 19, 'listen': [0] if requires_reply else [], 'player': [0], 'content': [json.dumps(notice)]}


def test_respawn_reenables_reply_without_resetting_transport(backend):
    module, messages = backend
    player = types.SimpleNamespace(status=module.PlayerStatus.Died)
    io = module.Communicate(types.SimpleNamespace(players=[player], player_property=[module.PlayDevice.MediaPlayer.value]))
    io.__send__([0], [0], [{'type': 'roundbegin'}])
    player.status = module.PlayerStatus.Alive
    io.__send__([0], [0], [{'type': 'roundbegin'}])
    assert [wire['listen'] for wire, _ in messages] == [[], [0]]

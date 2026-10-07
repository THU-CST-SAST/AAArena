"""Real AI9 transport isolation checks on a configured Linux evaluator host."""
import json
import os
from pathlib import Path
import socket
import sys

import pytest

from aa_arena.legacy.ai9_isolation import IsolatedAI9Player


@pytest.mark.skipif(sys.platform != "linux" or os.environ.get("AA_ARENA_TEST_HOST") != "1",
                    reason="requires configured systemd/Bubblewrap evaluator host")
def test_constructor_visibility_network_and_binary_transport(tmp_path):
    hidden = tmp_path / "host-private"
    hidden.write_text("private-marker")
    library = tmp_path / "own-library.so"
    library.write_bytes(b"own-library")
    loader = tmp_path / "fake-loader"
    loader.write_text('''#!/usr/bin/python3
import json, os, socket, sys
from pathlib import Path
hidden = Path(%r)
assert not hidden.exists(), "host files visible before player initialization"
assert Path('/workspace/player.so').read_bytes() == b'own-library'
assert not Path('/workspace/other-player.so').exists()
assert 'AA_ARENA_EVAL_TOKEN' not in os.environ
s = socket.socket(); s.settimeout(0.3)
try:
    s.connect(('198.51.100.1', 443))
except OSError:
    pass
else:
    raise AssertionError('external network reachable')
finally:
    s.close()
print('ISOLATION_PROBE_OK', file=sys.stderr, flush=True)
s = socket.socket(); s.bind(('127.0.0.1',0)); s.listen(1)
print(s.getsockname()[1], flush=True)
peer, _ = s.accept()
while True:
    data = peer.recv(65536)
    if not data: break
    peer.sendall(data)
''' % str(hidden))
    loader.chmod(0o755)
    output = tmp_path / "match"
    output.mkdir()
    player = IsolatedAI9Player(loader, library, output, 0)
    try:
        data = bytes(range(256)) * 512
        with socket.create_connection(("127.0.0.1", player.port), timeout=10) as client:
            client.sendall(data)
            received = bytearray()
            while len(received) < len(data):
                received.extend(client.recv(65536))
            assert received == data
        metadata = json.loads((output / "ailoader-0.isolation.json").read_text())
        assert metadata["launcher"] == "systemd_scope"
        assert metadata["control_group"]
    finally:
        player.close()
    assert "ISOLATION_PROBE_OK" in (output / "ailoader-0.stderr").read_text()
    cgroup = Path('/sys/fs/cgroup') / metadata['control_group'].lstrip('/') / 'cgroup.events'
    assert not cgroup.exists() or 'populated 0' in cgroup.read_text()

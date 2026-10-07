"""Real TCP regressions for judge SIGPIPE and loader EOF busy loops."""
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r'''
#include "socket.hh"
#include <arpa/inet.h>
#include <sys/socket.h>
#include <unistd.h>
#include <cstring>
#include <csignal>
#include <cassert>
int main(int argc, char **argv) {
    signal(SIGPIPE, SIG_DFL);
    int listener = socket(AF_INET, SOCK_STREAM, 0);
    sockaddr_in address{};
    address.sin_family = AF_INET;
    address.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    assert(bind(listener, (sockaddr*)&address, sizeof(address)) == 0);
    socklen_t length = sizeof(address);
    assert(getsockname(listener, (sockaddr*)&address, &length) == 0);
    assert(listen(listener, 1) == 0);
    int peer = socket(AF_INET, SOCK_STREAM, 0);
    assert(connect(peer, (sockaddr*)&address, sizeof(address)) == 0);
    int accepted = accept(listener, nullptr, nullptr);
    close(listener);
    SocketBase connection(accepted);
    char data[4]{};
    if (strcmp(argv[1], "roundtrip") == 0) {
        assert(::send(peer, "abcd", 4, 0) == 4);
        connection.recv(data, 4);
        assert(memcmp(data, "abcd", 4) == 0);
        connection.send("efgh", 4);
        assert(::recv(peer, data, 4, MSG_WAITALL) == 4);
        assert(memcmp(data, "efgh", 4) == 0);
        close(peer);
        return 0;
    }
    if (strcmp(argv[1], "eof") == 0) {
        assert(::send(peer, "a", 1, 0) == 1);
        close(peer);
        try { connection.recv(data, 4); }
        catch (const AI9Error&) { return 0; }
        return 2;
    }
    shutdown(accepted, SHUT_RDWR);
    close(peer);
    try { connection.send("abcd", 4); }
    catch (const AI9Error&) { return 0; }
    return 3;
}
'''


@pytest.fixture(scope='module')
def socket_probe(tmp_path_factory):
    directory = tmp_path_factory.mktemp('ai9-tcp')
    source = directory / 'probe.cc'
    source.write_text(HARNESS)
    shared = ROOT / 'games/lota/backend/platform_shared'
    binary = directory / 'probe'
    subprocess.run(['g++', '-std=c++14', '-O2', '-I', str(shared / 'include'),
        str(source), str(shared / 'socket.cc'), str(shared / 'ai9.cc'),
        '-o', str(binary)], check=True, capture_output=True, timeout=60)
    return binary


@pytest.mark.parametrize('mode', ['roundtrip', 'eof', 'closed_send'])
def test_disconnect_raises_without_signal_or_spin(socket_probe, mode):
    result = subprocess.run([str(socket_probe), mode], capture_output=True, timeout=3)
    assert result.returncode == 0, result.stderr.decode(errors='replace')


def test_all_ai9_backends_use_same_corrected_socket_implementation():
    reference = (ROOT / 'games/lota/backend/platform_shared/socket.cc').read_bytes()
    paths = [path for game in ('lota', 'dorado', 'monecraft', 'pacman')
        for path in (ROOT / 'games' / game / 'backend').rglob('socket.cc')]
    assert len(paths) == 6
    assert all(path.read_bytes() == reference for path in paths)

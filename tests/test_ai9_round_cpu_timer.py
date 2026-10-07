"""Exercise the real fork/ptrace loader through its native TCP protocol."""
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
HARNESS = r'''
#include "ailoader.hh"
#include <time.h>
#include <cstdlib>
double cpu() { timespec t; clock_gettime(CLOCK_THREAD_CPUTIME_ID, &t); return t.tv_sec + t.tv_nsec * 1e-9; }
class Game : public GameControllerBase {
    double work;
    unsigned long long iterations = 0;
    static void burn(unsigned long long n) { volatile unsigned long long sum = 0; for (unsigned long long i = 0; i < n; ++i) sum += i; }
public:
    explicit Game(double amount) : work(amount) {}
    void on_game_init() override { double start = cpu(); burn(10000000); double duration = cpu() - start; iterations = static_cast<unsigned long long>(10000000 * work / duration); }
    void on_round_start() override {}
    void do_round() override { if (work < 0) { volatile unsigned long long n = 0; for (;;) ++n; } else burn(iterations); }
    void on_round_end() override {}
    void on_game_end() override {}
    std::string get_log_msg() override { return ""; }
};
int main(int argc, char **argv) { Game game(std::atof(argv[1])); AILoader::mainloop(0, game); }
'''


def build_probe(backend, directory):
    source = directory / "probe.cc"; source.write_text(HARNESS)
    binary = directory / "probe"
    shared = backend / "platform_shared"
    subprocess.run(["g++", "-O2", "-std=c++14", "-pthread", "-w",
        "-I", str(backend / "libailoader/include"), "-I", str(shared / "include"),
        str(source), *map(str, sorted((backend / "libailoader").glob("*.cc"))),
        *map(str, sorted(shared.glob("*.cc"))), "-o", str(binary), "-ldl", "-lrt"],
        check=True, capture_output=True, timeout=60)
    return binary


@pytest.fixture(scope="module")
def loader_probe(tmp_path_factory):
    return build_probe(ROOT / "games/lota/backend", tmp_path_factory.mktemp("ai9-cpu-timer"))


def run_rounds(binary, work, count):
    process = subprocess.Popen([str(binary), str(work)], stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, start_new_session=True,
        env={**os.environ, "AI9_AILOADER_NOCLOSESTDIO": "1"})
    rows = []
    try:
        port = int(process.stdout.readline())
        with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
            connection.settimeout(5)
            connection.sendall(struct.pack("=I", 256 * 1024 * 1024))
            for _ in range(count):
                connection.sendall(struct.pack("=id", 0, .1))
                data = b""
                while len(data) < 152:
                    part = connection.recv(152 - len(data))
                    if not part:
                        raise AssertionError("loader closed before round result")
                    data += part
                status, cpu, wall = struct.unpack_from("=idd", data)
                rows.append((status, cpu, wall))
                if status != 0:
                    break
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        process.stdout.close()
    return rows


def test_many_cheap_rounds_never_hit_a_lifetime_cpu_limit(loader_probe):
    rows = run_rounds(loader_probe, .006, 600)
    assert len(rows) == 600, rows[-1]
    assert all(status == 0 and cpu < .1 for status, cpu, _ in rows)
    assert sum(cpu for _, cpu, _ in rows) > 3


@pytest.mark.parametrize("work", [.2, -1])
def test_real_cpu_overrun_and_infinite_loop_are_preempted(loader_probe, work):
    rows = run_rounds(loader_probe, work, 1)
    status, cpu, wall = rows[0]
    assert status == 1, rows
    assert .09 <= cpu < .5, rows
    assert wall < 5, rows


def test_all_four_games_share_the_corrected_loader():
    contents = [(ROOT / "games" / game / "backend/libailoader/ailoader.cc").read_bytes()
        for game in ("lota", "dorado", "monecraft", "pacman")]
    assert len(set(contents)) == 1

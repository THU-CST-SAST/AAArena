"""Loopback-to-Unix relay used inside the Codex network namespace."""

from __future__ import annotations

import argparse
import select
import signal
import socket
import socketserver
import subprocess
import threading


class Relay(socketserver.BaseRequestHandler):
    unix_socket = ""

    def handle(self) -> None:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
            upstream.connect(self.unix_socket)
            peers = (self.request, upstream)
            while True:
                readable, _, _ = select.select(peers, (), (), 60)
                if not readable:
                    continue
                for source in readable:
                    payload = source.recv(65536)
                    if not payload:
                        return
                    (upstream if source is self.request else self.request).sendall(payload)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--socket", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise SystemExit("missing App Server command")
    Relay.unix_socket = args.socket
    server = Server(("127.0.0.1", args.port), Relay)
    process = subprocess.Popen(command)

    def terminate(signum: int, _frame: object) -> None:
        if process.poll() is None:
            process.send_signal(signum)

    signal.signal(signal.SIGTERM, terminate)
    signal.signal(signal.SIGINT, terminate)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        return process.wait()
    finally:
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())

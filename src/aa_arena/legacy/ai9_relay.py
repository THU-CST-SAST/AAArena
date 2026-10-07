"""Trusted byte relay executed inside one player's filesystem/network sandbox.

Only the loader and this player's compiled library are mounted in /workspace.
The judge's TCP connection is carried over stdin/stdout; no host socket or
network namespace is exposed to the library, including its ELF constructors.
"""
from __future__ import annotations

import os
import selectors
import socket
import subprocess
import sys
import threading


def main() -> int:
    loader = subprocess.Popen(
        ["/workspace/ailoader", "/workspace/player.so"],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=None,
        env={"PATH": "/usr/bin:/bin", "AI9_AILOADER_NOCLOSESTDIO": "1",
             **({"AI9_AILOADER_NOSUPERVISOR": "1"} if sys.argv[1:] == ["unsupervised"] else {})},
    )
    assert loader.stdout is not None
    try:
        line = loader.stdout.readline(32)
        port = int(line.strip())
        if not 0 < port < 65536:
            raise ValueError("invalid loader port")
        # Loader diagnostics must never become bytes in the judge protocol.
        def drain() -> None:
            while data := loader.stdout.read(8192):
                os.write(2, data)
        threading.Thread(target=drain, daemon=True).start()
        with socket.create_connection(("127.0.0.1", port), timeout=30) as peer:
            peer.settimeout(None)
            os.write(1, b"READY\n")
            with selectors.DefaultSelector() as selector:
                selector.register(0, selectors.EVENT_READ)
                selector.register(peer, selectors.EVENT_READ)
                while True:
                    for key, _ in selector.select():
                        if key.fileobj == 0:
                            data = os.read(0, 65536)
                            if not data:
                                return 0
                            peer.sendall(data)
                        else:
                            data = peer.recv(65536)
                            if not data:
                                return 0
                            view = memoryview(data)
                            while view:
                                view = view[os.write(1, view):]
    except (ConnectionResetError, BrokenPipeError):
        # A judge closing an adjudicated match terminates the byte stream.
        return 0
    finally:
        if loader.poll() is None:
            loader.kill()
        loader.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

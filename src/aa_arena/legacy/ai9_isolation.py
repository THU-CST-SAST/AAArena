"""Per-player AI9 isolation with a loopback-only judge bridge."""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import shutil
import socket
import threading

from aa_arena.sandbox.model import ProcessSpec, SandboxInfrastructureError
from aa_arena.sandbox.systemd import SystemdScopeLauncher


class IsolatedAI9Player:
    def __init__(self, loader: Path, library: Path, directory: Path, index: int,
                 *, supervised: bool = True, timeout: float = 30):
        self.process = None
        self.listener = None
        self.peer = None
        self.threads = []
        self.error = None
        self.stopping = threading.Event()
        self.stderr = (directory / f"ailoader-{index}.stderr").open("wb")
        workspace = directory / f"isolated-player-{index}"
        workspace.mkdir()
        for source, name in [(loader, "ailoader"), (library, "player.so"),
                             (Path(__file__).with_name("ai9_relay.py"), "relay.py")]:
            shutil.copy2(source, workspace / name)
        try:
            launcher = SystemdScopeLauncher()
            self.process = launcher.start(
                ProcessSpec(("/usr/bin/python3", "-I", "relay.py",
                             *( () if supervised else ("unsupervised",))), workspace),
                match_id=directory.name, player_index=index,
            )
            (directory / f"ailoader-{index}.isolation.json").write_text(
                json.dumps(vars(self.process.metadata), indent=2) + "\n")
            self._thread(self._stderr)
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout, selectors.EVENT_READ)
                if not selector.select(timeout) or self.process.stdout.readline(32) != b"READY\n":
                    raise SandboxInfrastructureError("isolated AI9 loader did not become ready")
            self.listener = socket.socket()
            self.listener.bind(("127.0.0.1", 0))
            self.listener.listen(1)
            self.listener.settimeout(timeout)
            self.port = self.listener.getsockname()[1]
            self._thread(self._bridge)
        except Exception:
            self.close()
            raise

    def _thread(self, target):
        thread = threading.Thread(target=target, daemon=True)
        self.threads.append(thread)
        thread.start()

    def _stderr(self):
        try:
            while data := os.read(self.process.stderr.fileno(), 8192):
                self.stderr.write(data)
                self.stderr.flush()
        except (OSError, ValueError):
            pass

    def _bridge(self):
        try:
            self.peer, _ = self.listener.accept()
            with selectors.DefaultSelector() as selector:
                selector.register(self.peer, selectors.EVENT_READ)
                selector.register(self.process.stdout, selectors.EVENT_READ)
                while not self.stopping.is_set():
                    for key, _ in selector.select(0.5):
                        if key.fileobj is self.peer:
                            data = self.peer.recv(65536)
                            if not data:
                                return
                            view = memoryview(data)
                            while view:
                                view = view[os.write(self.process.stdin.fileno(), view):]
                        else:
                            data = os.read(self.process.stdout.fileno(), 65536)
                            if not data:
                                return
                            self.peer.sendall(data)
        except (OSError, ValueError) as exc:
            if not self.stopping.is_set():
                self.error = str(exc)
        finally:
            if self.peer:
                try:
                    self.peer.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def close(self):
        self.stopping.set()
        if self.listener:
            self.listener.close()
        if self.peer:
            try:
                self.peer.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.peer.close()
        clean = self.process.cleanup() if self.process else None
        for thread in self.threads:
            thread.join(timeout=2)
        self.stderr.close()
        if clean and not clean.clean:
            raise SandboxInfrastructureError("AI9 player cleanup failed: " + clean.detail)

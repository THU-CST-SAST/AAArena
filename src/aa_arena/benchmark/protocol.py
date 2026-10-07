"""Bounded newline-delimited JSON-RPC client for Codex App Server."""

from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


_CREDENTIAL = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}")


class AppServerProtocolError(RuntimeError):
    pass


class JsonRpcStdioClient:
    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        stderr_path: Path,
        secrets: tuple[str, ...] = (),
        request_timeout_s: float = 30.0,
    ) -> None:
        self.request_timeout_s = request_timeout_s
        self.stderr_path = stderr_path
        self.secrets = tuple(value for value in secrets if value)
        self._next_id = 1
        self._responses: dict[int, dict[str, Any]] = {}
        self._condition = threading.Condition()
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._closed = False
        self.process = subprocess.Popen(
            tuple(command),
            cwd=cwd,
            env=dict(environment),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            start_new_session=True,
        )
        if not self.process.stdin or not self.process.stdout or not self.process.stderr:
            self.process.terminate()
            raise AppServerProtocolError("failed to open Codex App Server stdio")
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _redact(self, text: str) -> str:
        value = _CREDENTIAL.sub("[REDACTED]", text)
        for secret in self.secrets:
            value = value.replace(secret, "[REDACTED]")
        return value

    def send(self, message: Mapping[str, object]) -> None:
        if not self.process.stdin or self.process.poll() is not None:
            raise AppServerProtocolError("Codex App Server is not running")
        self.process.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: Mapping[str, object], timeout_s: float | None = None) -> Mapping[str, Any]:
        with self._condition:
            request_id = self._next_id
            self._next_id += 1
        self.send({"id": request_id, "method": method, "params": dict(params)})
        deadline = time.monotonic() + (timeout_s or self.request_timeout_s)
        with self._condition:
            while request_id not in self._responses:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"Codex App Server request timed out: {method}")
                if self._closed:
                    raise AppServerProtocolError(f"Codex App Server closed while waiting for {method}")
                self._condition.wait(remaining)
            response = self._responses.pop(request_id)
        if "error" in response:
            raise AppServerProtocolError(
                f"Codex App Server rejected {method}: {self._redact(str(response['error']))}"
            )
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise AppServerProtocolError(f"invalid Codex response for {method}")
        return result

    def notify(self, method: str, params: Mapping[str, object] | None = None) -> None:
        message: dict[str, object] = {"method": method}
        if params is not None:
            message["params"] = dict(params)
        self.send(message)

    def respond(self, request_id: object, result: Mapping[str, object]) -> None:
        self.send({"id": request_id, "result": dict(result)})

    def next_message(self, timeout_s: float) -> dict[str, Any]:
        try:
            return self.messages.get(timeout=timeout_s)
        except queue.Empty as exc:
            if self._closed:
                raise AppServerProtocolError("Codex App Server closed while waiting for a notification") from exc
            raise TimeoutError("timed out waiting for Codex App Server") from exc

    def _read_stdout(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                if not line.strip():
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    self.messages.put({"method": "protocol/error", "params": {"message": str(exc)}})
                    continue
                if not isinstance(message, dict):
                    continue
                if "id" in message and "method" not in message:
                    with self._condition:
                        self._responses[int(message["id"])] = message
                        self._condition.notify_all()
                else:
                    self.messages.put(message)
        finally:
            with self._condition:
                self._closed = True
                self._condition.notify_all()

    def _read_stderr(self) -> None:
        assert self.process.stderr is not None
        self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
        with self.stderr_path.open("a", encoding="utf-8") as stream:
            for line in self.process.stderr:
                stream.write(self._redact(line))
                stream.flush()

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        with self._condition:
            self._closed = True
            self._condition.notify_all()

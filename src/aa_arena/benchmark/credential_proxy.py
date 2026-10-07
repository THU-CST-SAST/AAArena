"""Minimal Responses reverse proxy that keeps the real key outside Codex's process."""

from __future__ import annotations

import argparse
import http.server
import json
import os
import pwd
import socketserver
import sys
import time
import threading
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests


class RateLimitedTransport:
    """Pace requests and retry explicit transient HTTP rejections, never SSE."""
    def __init__(self, min_interval=0.0, max_retries=0):
        if not 0 <= min_interval <= 300 or not 0 <= max_retries <= 6:
            raise ValueError('Invalid bounded rate-limit policy')
        self.min_interval, self.max_retries = min_interval, max_retries
        self.last_started = None
        self.lock = threading.Lock()

    @staticmethod
    def delay(response, attempt):
        retry_after = response.headers.get('Retry-After')
        if retry_after:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                try:
                    return max(0.0, parsedate_to_datetime(retry_after).timestamp()-time.time())
                except (ValueError, TypeError, OverflowError):
                    pass
        return min(300.0, 30.0 * 2**attempt)

    def post(self, target, **kwargs):
        with self.lock:
            for attempt in range(self.max_retries+1):
                if self.last_started is not None:
                    wait = self.min_interval-(time.monotonic()-self.last_started)
                    if wait > 0:
                        time.sleep(wait)
                self.last_started = time.monotonic()
                response = requests.post(target, **kwargs)
                if response.status_code not in {429,502,503,504} or attempt == self.max_retries:
                    return response
                wait = self.delay(response, attempt)
                if wait > 900:
                    # Return the rejection instead of retrying before a long
                    # server cooldown expires or hiding an unbounded wait.
                    return response
                print('upstream_rate_backoff='+json.dumps({'attempt':attempt+1,'seconds':wait,'http_status':response.status_code}),file=sys.stderr,flush=True)
                response.close()
                time.sleep(wait)
        raise AssertionError('Rate-limit retry loop did not return')


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    upstream = ""
    api_key = ""
    expected_model = ""
    transport = RateLimitedTransport()

    @staticmethod
    def _terminal_usage(payload: bytes) -> list[dict[str, object]]:
        """Audit provider-reported usage even when Codex rejects an incomplete response.

        Never log prompts, outputs, headers, credentials, or arbitrary error text.
        This is a separate request-level billing audit, not an additive ledger
        update (successful calls are already in Codex's cumulative usage).
        """
        rows = []
        for line in payload.splitlines():
            if not line.startswith(b"data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(event, dict) or event.get("type") not in {
                "response.completed", "response.incomplete", "response.failed"
            }:
                continue
            response = event.get("response") or {}
            usage = response.get("usage") or {}
            numeric = {k: usage[k] for k in ("input_tokens", "output_tokens", "total_tokens")
                       if isinstance(usage.get(k), int)}
            for key in ("input_tokens_details", "output_tokens_details"):
                details = usage.get(key) or {}
                numeric[key] = {k: details[k] for k in ("cached_tokens", "reasoning_tokens")
                                if isinstance(details.get(k), int)}
            rows.append({"event": event["type"], "usage": numeric,
                         "usage_reported": bool(usage),
                         "output_limited": (response.get("incomplete_details") or {}).get("reason") == "max_output_tokens"})
        return rows

    @staticmethod
    def _response_models(payload: bytes) -> set[str]:
        """Extract the model identity from buffered Responses API SSE events."""

        models: set[str] = set()
        for raw_line in payload.splitlines():
            if not raw_line.startswith(b"data:"):
                continue
            data = raw_line.removeprefix(b"data:").strip()
            if not data or data == b"[DONE]":
                continue
            try:
                event = json.loads(data)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(event, dict):
                continue
            response = event.get("response")
            if isinstance(response, dict) and isinstance(response.get("model"), str):
                models.add(response["model"])
            if isinstance(event.get("model"), str):
                models.add(event["model"])
        return models

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length)
        try:
            request = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            request = None
        requested_model = request.get("model") if isinstance(request, dict) else None
        if isinstance(request, dict):
            input_value = request.get("input")
            print("upstream_request=" + json.dumps({
                "input_items": len(input_value) if isinstance(input_value, list) else None,
                "input_bytes": len(json.dumps(input_value, ensure_ascii=False).encode()),
                "max_output_tokens": request.get("max_output_tokens"),
                "reasoning_effort": (request.get("reasoning") or {}).get("effort")
                    if isinstance(request.get("reasoning"), dict) and
                    (request.get("reasoning") or {}).get("effort") in
                    {"none", "minimal", "low", "medium", "high", "xhigh", "max"} else None,
            }), file=sys.stderr, flush=True)
        if requested_model is not None and requested_model != self.expected_model:
            payload = (
                f"model mismatch: requested {requested_model!r}, expected "
                f"{self.expected_model!r}"
            ).encode()
            self.send_response(400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        target = self.upstream.rstrip("/") + self.path
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in {"host", "authorization", "content-length", "connection"}
        }
        headers["Authorization"] = f"Bearer {self.api_key}"
        try:
            response = self.transport.post(
                target,
                data=body,
                headers=headers,
                timeout=3600,
                verify="/etc/ssl/certs/ca-certificates.crt",
            )
            payload = response.content
            for usage in self._terminal_usage(payload):
                print("upstream_usage=" + json.dumps(usage), file=sys.stderr, flush=True)
            status = response.status_code
            response_headers = response.headers
            if status < 400 and self.path.rstrip("/").endswith("/responses"):
                observed = self._response_models(payload)
                rendered = ",".join(sorted(observed)) if observed else "<missing>"
                print(
                    f"upstream_response_models={rendered} expected={self.expected_model}",
                    file=sys.stderr,
                    flush=True,
                )
                if observed != {self.expected_model}:
                    payload = (
                        f"upstream model identity mismatch: observed {rendered}, "
                        f"expected {self.expected_model}"
                    ).encode()
                    status = 502
                    response_headers = {}
            self.send_response(status)
            for key, value in response_headers.items():
                if key.lower() not in {"transfer-encoding", "connection", "content-length"}:
                    self.send_header(key, value)
        except (OSError, requests.RequestException) as error:
            payload = f"upstream request failed: {type(error).__name__}: {error}".encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
        print(format % args, file=sys.stderr, flush=True)


class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--expected-model", required=True)
    parser.add_argument("--drop-user", default="daemon")
    parser.add_argument("--min-request-interval", type=float, default=0.0)
    parser.add_argument("--rate-limit-retries", type=int, default=0)
    args = parser.parse_args(argv)
    key = sys.stdin.readline().strip()
    if not key:
        raise SystemExit("credential proxy received no API key")
    Handler.upstream = args.upstream
    Handler.api_key = key
    Handler.expected_model = args.expected_model
    Handler.transport = RateLimitedTransport(args.min_request_interval,args.rate_limit_retries)
    socket_path = Path(args.socket).resolve()
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.unlink(missing_ok=True)
    server = ThreadingUnixHTTPServer(str(socket_path), Handler)
    socket_path.chmod(0o666)
    if os.geteuid() == 0:
        account = pwd.getpwnam(args.drop_user)
        os.setgroups([])
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    print(f"unix:{socket_path}", flush=True)
    try:
        server.serve_forever()
    finally:
        socket_path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

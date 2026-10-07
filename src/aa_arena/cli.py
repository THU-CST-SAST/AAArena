"""Unified command line for resources, native benchmark runs, and reports."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from aa_arena.benchmark.profile import ModelProfile, load_profile
from aa_arena.benchmark.runtime import CodexArenaRuntime
from aa_arena.benchmark.service import BenchmarkService
from aa_arena.benchmark.trajectory import compare_reports, rebuild_report
from aa_arena.io import atomic_write_json
from aa_arena.resources import ARENA_GAMES, certify, certify_all


def _resources(args: argparse.Namespace) -> int:
    output = Path(args.output).resolve() if args.output else None
    results = (
        certify_all(output_root=output, live=not args.skip_live)
        if args.all
        else [certify(args.game, output_root=output, live=not args.skip_live)]
    )
    print(json.dumps([result.__dict__ for result in results], ensure_ascii=False, indent=2))
    return 0


def _service(args: argparse.Namespace) -> BenchmarkService:
    return BenchmarkService(
        Path(args.run_dir),
        game=args.game,
        model_profile=args.model_profile,
        small_budget=args.small_budget,
        large_budget=args.large_budget,
        workers=args.workers,
        extend_budget_once=getattr(args, "extend_budget_once", False),
    )


def _api_key(profile_name: str) -> tuple[ModelProfile, str]:
    profile = load_profile(profile_name)
    key = os.environ.get(profile.api_key_env)
    if not key:
        raise RuntimeError(f"model API key is missing: export {profile.api_key_env}")
    return profile, key


def _compatibility_identity(profile: ModelProfile, codex_binary: str) -> dict[str, object]:
    resolved = shutil.which(codex_binary)
    if resolved is None:
        raise FileNotFoundError(f"Codex binary not found: {codex_binary}")
    version = subprocess.run(
        (resolved, "--version"),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    ).stdout.strip()
    return {
        "tool_contract_version": 2,
        "runtime_sha256": hashlib.sha256(
            Path(__file__).with_name("benchmark").joinpath("runtime.py").read_bytes()
            + Path(__file__).with_name("benchmark").joinpath("service.py").read_bytes()
        ).hexdigest(),
        "profile": profile.name,
        "model": profile.model,
        "provider": profile.model_provider,
        "endpoint_sha256": hashlib.sha256(profile.base_url.encode()).hexdigest(),
        "wire_api": profile.wire_api,
        "reasoning_effort": profile.reasoning_effort,
        "context_window": profile.context_window,
        "effective_context_window_percent": profile.effective_context_window_percent,
        "codex_binary": str(Path(resolved).resolve()),
        "codex_version": version,
    }


def _compatible_record(path: Path, identity: dict[str, object]) -> bool:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(record, dict):
        return False
    checks = record.get("checks")
    return (
        record.get("identity") == identity
        and isinstance(checks, dict)
        and bool(checks)
        and all(value is True for value in checks.values())
    )


def _require_benchmark_runtime() -> None:
    """Fail before model or match work if mandatory report dependencies are absent."""

    try:
        import matplotlib  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "benchmark run requires trajectory support: pip install -e '.[benchmark]'"
        ) from exc


def _benchmark_run(args: argparse.Namespace) -> int:
    _require_benchmark_runtime()
    profile, key = _api_key(args.model_profile)
    compatibility_path = Path(args.run_dir).resolve() / "controller" / "compatibility.json"
    identity = _compatibility_identity(profile, args.codex_binary)
    if not args.skip_doctor and not _compatible_record(compatibility_path, identity):
        with tempfile.TemporaryDirectory(prefix="aa-arena-codex-doctor-") as temporary:
            with BenchmarkService(
                Path(temporary),
                game=args.game,
                model_profile=args.model_profile,
                small_budget=1,
                large_budget=1,
                workers=1,
            ) as doctor_service:
                doctor_service.initialize_workspace()
                with CodexArenaRuntime(
                    doctor_service,
                    profile,
                    api_key=key,
                    codex_binary=args.codex_binary,
                ) as doctor_runtime:
                    compatibility = doctor_runtime.compatibility_gate()
        atomic_write_json(
            compatibility_path,
            {"identity": identity, "checks": compatibility},
        )
        print(json.dumps({"compatibility": compatibility}, ensure_ascii=False), flush=True)
    with _service(args) as service:
        service.initialize_workspace()
        recovered = service.recover_pending()
        if recovered is not None:
            print(json.dumps({"recovered_match": recovered}, ensure_ascii=False), flush=True)
        if not args.skip_baseline and not any(
            row["kind"] == "baseline" and row["status"] == "complete"
            for row in service.ledger.submissions()
        ):
            result = service.baseline()
            print(json.dumps({"baseline": result}, ensure_ascii=False), flush=True)
        with CodexArenaRuntime(
            service,
            profile,
            api_key=key,
            codex_binary=args.codex_binary,
        ) as runtime:
            runtime.run()
    print(json.dumps(rebuild_report(Path(args.run_dir)), ensure_ascii=False, indent=2))
    return 0


def _benchmark_doctor(args: argparse.Namespace) -> int:
    profile, key = _api_key(args.model_profile)
    with _service(args) as service:
        service.initialize_workspace()
        with CodexArenaRuntime(
            service,
            profile,
            api_key=key,
            codex_binary=args.codex_binary,
        ) as runtime:
            print(json.dumps(runtime.compatibility_gate(), ensure_ascii=False, indent=2))
    return 0


def _benchmark_report(args: argparse.Namespace) -> int:
    print(json.dumps(rebuild_report(Path(args.run_dir)), ensure_ascii=False, indent=2))
    return 0


def _benchmark_compare(args: argparse.Namespace) -> int:
    output = compare_reports((Path(item) for item in args.run_dirs), Path(args.output))
    print(output)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aa-arena")
    commands = parser.add_subparsers(dest="command", required=True)

    resources = commands.add_parser("resources", help="Build and certify agent-visible resources")
    resource_commands = resources.add_subparsers(dest="resource_command", required=True)
    certify_parser = resource_commands.add_parser("certify")
    selection = certify_parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--game", choices=ARENA_GAMES)
    selection.add_argument("--all", action="store_true")
    certify_parser.add_argument("--output")
    certify_parser.add_argument(
        "--skip-live",
        action="store_true",
        help="Only run static bundle checks; live Saiblo SDK matches are the default",
    )
    certify_parser.set_defaults(handler=_resources)

    benchmark = commands.add_parser("benchmark", help="Run or report a persistent Codex benchmark")
    benchmark_commands = benchmark.add_subparsers(dest="benchmark_command", required=True)
    for name, handler in (("run", _benchmark_run), ("doctor", _benchmark_doctor)):
        sub = benchmark_commands.add_parser(name)
        sub.add_argument("--game", choices=ARENA_GAMES, default="antwar")
        sub.add_argument("--model-profile", default="default")
        sub.add_argument("--small-budget", type=int, default=128)
        sub.add_argument("--large-budget", type=int, default=16)
        # The benchmark host has 32 cores; four workers under-utilised it and
        # made large fixed-pool evaluations (notably AntWar2) appear hung.
        sub.add_argument("--workers", type=int, default=16)
        sub.add_argument("--run-dir", required=True)
        sub.add_argument("--codex-binary", default="codex")
        if name == "run":
            sub.add_argument("--skip-doctor", action="store_true")
            sub.add_argument("--extend-budget-once", action="store_true", help="Add the specified small/large budget once to the completed ledger without resetting usage or session")
            sub.add_argument("--skip-baseline", action="store_true")
        sub.set_defaults(handler=handler)
    report = benchmark_commands.add_parser("report")
    report.add_argument("run_dir")
    report.set_defaults(handler=_benchmark_report)
    compare = benchmark_commands.add_parser("compare")
    compare.add_argument("run_dirs", nargs="+")
    compare.add_argument("--output", default="trajectory-comparison.png")
    compare.set_defaults(handler=_benchmark_compare)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.handler(args))
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

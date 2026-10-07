"""Append-only trajectory events and reproducible offline reports."""

from __future__ import annotations

import csv
import html
import json
import os
from pathlib import Path
from typing import Any, Iterable

from aa_arena.io import atomic_write_json, atomic_write_text


POINT_COLUMNS = (
    "checkpoint",
    "snapshot_id",
    "parent_snapshot_id",
    "strategy_hash",
    "active_seconds",
    "wall_seconds",
    "total_tokens",
    "small_budget_used",
    "large_budget_used",
    "elo",
    "elo_ci_low",
    "elo_ci_high",
    "rank",
    "pool_win_rate",
    "wins",
    "draws",
    "losses",
    "candidate_errors",
    "changed_files",
    "lines_added",
    "lines_deleted",
)


class TrajectoryLog:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.events_path = self.directory / "events.jsonl"

    def read(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        rows = []
        for number, line in enumerate(self.events_path.read_text(encoding="utf-8").splitlines(), 1):
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid trajectory event line {number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"trajectory event line {number} is not an object")
            rows.append(value)
        return rows

    def append(self, event: dict[str, Any]) -> None:
        submission_id = event.get("submission_id")
        if submission_id and any(row.get("submission_id") == submission_id for row in self.read()):
            raise ValueError(f"trajectory already contains submission {submission_id}")
        payload = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        descriptor = os.open(
            self.events_path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT,
            0o600,
        )
        try:
            os.write(descriptor, (payload + "\n").encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def current_generation(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only the latest run generation from an append-only trajectory log."""

    rows = list(events)
    baseline_indexes = [
        index for index, event in enumerate(rows) if event.get("kind") == "baseline"
    ]
    return rows[baseline_indexes[-1] :] if baseline_indexes else rows


def performance_points(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    points = []
    checkpoint = 0
    for event in current_generation(events):
        if event.get("status") != "complete" or event.get("kind") not in {"baseline", "large"}:
            continue
        result = event.get("result") or {}
        snapshot = event.get("snapshot") or {}
        budgets = event.get("budgets") or {}
        usage = event.get("token_usage") or {}
        point = {
            "checkpoint": checkpoint,
            "snapshot_id": snapshot.get("snapshot_id"),
            "parent_snapshot_id": snapshot.get("parent_snapshot_id"),
            "strategy_hash": snapshot.get("strategy_hash"),
            "active_seconds": event.get("active_seconds", 0),
            "wall_seconds": event.get("wall_seconds", 0),
            "total_tokens": usage.get("total_tokens", 0),
            "small_budget_used": budgets.get("small_used", 0),
            "large_budget_used": budgets.get("large_used", 0),
            "elo": result.get("elo"),
            "elo_ci_low": result.get("elo_ci_low"),
            "elo_ci_high": result.get("elo_ci_high"),
            "rank": result.get("rank"),
            "pool_win_rate": result.get("pool_win_rate"),
            "wins": result.get("wins", 0),
            "draws": result.get("draws", 0),
            "losses": result.get("losses", 0),
            "candidate_errors": result.get("candidate_errors", 0),
            "changed_files": snapshot.get("changed_files", 0),
            "lines_added": snapshot.get("lines_added", 0),
            "lines_deleted": snapshot.get("lines_deleted", 0),
        }
        points.append(point)
        checkpoint += 1
    return points


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=POINT_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _plot(path: Path, points: list[dict[str, Any]]) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:  # pragma: no cover - depends on optional installation
        raise RuntimeError("trajectory PNG requires: pip install -e '.[benchmark]'") from exc
    figure, axes = plt.subplots(2, 4, figsize=(18, 8), constrained_layout=True)
    x_specs = (
        ("checkpoint", "Large-match checkpoint"),
        ("total_tokens", "Cumulative tokens"),
        ("active_seconds", "Agent active seconds"),
        ("small_budget_used", "Small-match opponents used"),
    )
    elo = [float(point["elo"]) for point in points]
    low = [float(point["elo_ci_low"]) for point in points]
    high = [float(point["elo_ci_high"]) for point in points]
    rank = [int(point["rank"]) for point in points]
    for column, (field, label) in enumerate(x_specs):
        x = [float(point[field]) for point in points]
        axes[0, column].plot(x, elo, marker="o", color="#1565c0")
        axes[0, column].fill_between(x, low, high, alpha=0.2, color="#1565c0")
        axes[0, column].set_ylabel("Candidate Elo")
        axes[0, column].set_xlabel(label)
        axes[0, column].grid(alpha=0.25)
        axes[1, column].step(x, rank, where="post", color="#c62828")
        axes[1, column].scatter(x, rank, color="#c62828", s=18)
        axes[1, column].invert_yaxis()
        axes[1, column].set_ylabel("Pool rank (lower is better)")
        axes[1, column].set_xlabel(label)
        axes[1, column].grid(alpha=0.25)
    figure.suptitle("AA-Arena measured iteration trajectory")
    figure.savefig(path, dpi=160)
    plt.close(figure)


def _plot_diagnostics(path: Path, events: list[dict[str, Any]]) -> None:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("trajectory diagnostics require matplotlib") from exc

    figure, axes = plt.subplots(3, 2, figsize=(14, 12), constrained_layout=True)
    sequence = [int(event.get("sequence") or index + 1) for index, event in enumerate(events)]
    small_used = [int((event.get("budgets") or {}).get("small_used", 0)) for event in events]
    large_used = [int((event.get("budgets") or {}).get("large_used", 0)) for event in events]
    axes[0, 0].step(sequence, small_used, where="post", label="small opponents")
    axes[0, 0].step(sequence, large_used, where="post", label="large matches")
    axes[0, 0].set_title("Budget consumption timeline")
    axes[0, 0].legend()

    small_index: list[int] = []
    small_rank: list[int] = []
    small_score: list[float] = []
    for event in events:
        if event.get("kind") != "small":
            continue
        for seat in (event.get("result") or {}).get("seats", []):
            if seat.get("opponent_rank") is None or seat.get("score") is None:
                continue
            small_index.append(len(small_index) + 1)
            small_rank.append(int(seat["opponent_rank"]))
            small_score.append(float(seat["score"]))
    if small_index:
        scatter = axes[0, 1].scatter(
            small_index, small_rank, c=small_score, cmap="RdYlGn", vmin=0, vmax=1
        )
        figure.colorbar(scatter, ax=axes[0, 1], label="candidate score")
        axes[0, 1].invert_yaxis()
    else:
        axes[0, 1].text(0.5, 0.5, "No small matches", ha="center", va="center")
    axes[0, 1].set_title("Small-match result and selected opponent rank")
    axes[0, 1].set_xlabel("Seat attempt")
    axes[0, 1].set_ylabel("Opponent rank")

    tokens = [int((event.get("token_usage") or {}).get("total_tokens", 0)) for event in events]
    active = [float(event.get("active_seconds", 0)) for event in events]
    token_delta = [
        value - (tokens[index - 1] if index else 0) for index, value in enumerate(tokens)
    ]
    active_delta = [
        value - (active[index - 1] if index else 0) for index, value in enumerate(active)
    ]
    axes[1, 0].bar(sequence, token_delta, color="#1565c0")
    axes[1, 0].set_title("Token increment per submission")
    active_axis = axes[1, 0].twinx()
    active_axis.plot(sequence, active_delta, color="#ef6c00", marker="o")
    active_axis.set_ylabel("Active-time increment (s)")

    added = [int((event.get("snapshot") or {}).get("lines_added", 0)) for event in events]
    deleted = [int((event.get("snapshot") or {}).get("lines_deleted", 0)) for event in events]
    changed = [int((event.get("snapshot") or {}).get("changed_files", 0)) for event in events]
    axes[1, 1].bar(sequence, added, label="lines added", color="#2e7d32")
    axes[1, 1].bar(sequence, [-value for value in deleted], label="lines deleted", color="#c62828")
    change_axis = axes[1, 1].twinx()
    change_axis.plot(sequence, changed, label="changed files", color="#6a1b9a", marker=".")
    axes[1, 1].set_title("Strategy/workspace change size")
    axes[1, 1].legend(loc="upper left")
    change_axis.set_ylabel("Changed files")

    errors = [int((event.get("result") or {}).get("candidate_errors", 0)) for event in events]
    retries = [
        int((event.get("result") or {}).get("infrastructure_retries", 0)) for event in events
    ]
    axes[2, 0].bar(sequence, errors, label="candidate errors", color="#c62828")
    axes[2, 0].bar(sequence, retries, bottom=errors, label="infra retries", color="#f9a825")
    axes[2, 0].set_title("Candidate errors and infrastructure retries")
    axes[2, 0].legend()

    wins = [int((event.get("result") or {}).get("wins", 0)) for event in events]
    draws = [int((event.get("result") or {}).get("draws", 0)) for event in events]
    losses = [int((event.get("result") or {}).get("losses", 0)) for event in events]
    axes[2, 1].bar(sequence, wins, label="wins", color="#2e7d32")
    axes[2, 1].bar(sequence, draws, bottom=wins, label="draws", color="#9e9e9e")
    axes[2, 1].bar(
        sequence,
        losses,
        bottom=[win + draw for win, draw in zip(wins, draws, strict=True)],
        label="losses",
        color="#c62828",
    )
    axes[2, 1].set_title("Outcomes by submission")
    axes[2, 1].legend()
    for axis in axes.flat:
        axis.grid(alpha=0.2)
        axis.set_xlabel("Submission sequence")
    figure.suptitle("AA-Arena iteration diagnostics (not official Elo points)")
    figure.savefig(path, dpi=160)
    plt.close(figure)


def rebuild_report(run_root: Path) -> dict[str, Any]:
    run_root = Path(run_root).resolve()
    directory = run_root / "trajectory"
    log = TrajectoryLog(directory)
    all_events = log.read()
    events = current_generation(all_events)
    points = performance_points(events)
    atomic_write_json(directory / "points.json", points)
    _write_csv(directory / "points.csv", points)
    last_evaluated = points[-1] if points else None
    final = last_evaluated
    champion_events = [event for event in events if (event.get("result") or {}).get("champion")]
    if champion_events:
        champion = (champion_events[-1].get("result") or {}).get("champion") or {}
        champion_id = champion.get("snapshot_id")
        final = next(
            (point for point in reversed(points) if point["snapshot_id"] == champion_id),
            last_evaluated,
        )
    from aa_arena.benchmark.distribution import scope
    summary = {
        **scope(),
        "schema_version": 1,
        "events": len(events),
        "archived_events": len(all_events) - len(events),
        "performance_points": len(points),
        "baseline_present": bool(points and points[0]["checkpoint"] == 0),
        "best_elo": max((point["elo"] for point in points), default=None),
        "best_rank": min((point["rank"] for point in points), default=None),
        "last_evaluated": last_evaluated,
        "final": final,
    }
    atomic_write_json(directory / "summary.json", summary)
    if points:
        _plot(directory / "trajectory.png", points)
    if events:
        _plot_diagnostics(directory / "diagnostics.png", events)
    rows = "".join(
        "<tr>"
        + "".join(f"<td>{html.escape(str(point.get(column, '')))}</td>" for column in POINT_COLUMNS)
        + "</tr>"
        for point in points
    )
    document = f"""<!doctype html>
<meta charset="utf-8"><title>AA-Arena trajectory</title>
<style>body{{font-family:sans-serif;margin:2rem}}img{{max-width:100%}}table{{border-collapse:collapse;font-size:12px}}td,th{{border:1px solid #ccc;padding:4px}}</style>
<h1>AA-Arena iteration trajectory</h1>
<p>Published-subset results: the starter baseline followed by local large matches. These are not complete-pool paper scores.</p>
{'<img src="trajectory.png" alt="trajectory">' if points else "<p>No performance points yet.</p>"}
<h2>Diagnostics</h2>
{'<img src="diagnostics.png" alt="diagnostics">' if events else "<p>No submission events yet.</p>"}
<h2>Measured points</h2><table><thead><tr>{"".join(f"<th>{column}</th>" for column in POINT_COLUMNS)}</tr></thead><tbody>{rows}</tbody></table>
<h2>All submission events</h2><p>See <a href="events.jsonl">events.jsonl</a>.</p>
"""
    atomic_write_text(directory / "report.html", document)
    return summary


def compare_reports(run_roots: Iterable[Path], output: Path) -> Path:
    roots = tuple(Path(root).resolve() for root in run_roots)
    if len(roots) < 2:
        raise ValueError("benchmark compare requires at least two run directories")
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("trajectory comparison requires matplotlib") from exc
    figure, axis = plt.subplots(figsize=(10, 6), constrained_layout=True)
    for root in roots:
        points = performance_points(TrajectoryLog(root / "trajectory").read())
        axis.plot(
            [point["total_tokens"] for point in points],
            [point["elo"] for point in points],
            marker="o",
            label=root.name,
        )
    axis.set_xlabel("Cumulative tokens")
    axis.set_ylabel("Candidate Elo")
    axis.grid(alpha=0.25)
    axis.legend()
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=160)
    plt.close(figure)
    return output

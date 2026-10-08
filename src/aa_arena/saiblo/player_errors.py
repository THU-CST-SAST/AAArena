"""Attribute official forfeits using the judger's transport evidence."""
import json
from pathlib import Path

def transport_player_errors(
    events_path: Path | None, *, roles: tuple[str, ...] = ("P0", "P1")
) -> tuple[list[str], str | None]:
    failed_roles: set[str] = set()
    failures: list[str] = []
    if events_path is not None and events_path.is_file():
        with events_path.open(encoding="utf-8") as events:
            for line in events:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("kind") == "game_over":
                    break
                if event.get("kind") != "ai_error":
                    continue
                player = event.get("player")
                if type(player) is int and 0 <= player < len(roles):
                    role = roles[player]
                    failed_roles.add(role)
                    failures.append(f"{role}: {event.get('error_log', 'player_error')}")
    return sorted(failed_roles), "; ".join(dict.fromkeys(failures)) if failures else None

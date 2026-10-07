"""Public local evaluation scope."""
import json
from pathlib import Path
from aa_arena.elo.model import repository_root


def local_subset(root: Path | None = None) -> bool:
    path = (root or repository_root()) / "configs/distribution.json"
    return path.is_file() and json.loads(path.read_text()).get("local_evaluation") == "published-subset"


def scope(root: Path | None = None, *, formal: bool = False) -> dict:
    if formal:
        return {"evaluation_scope": "full-pool", "official_full_pool": True,
                "rank_basis": "frozen complete reference pool"}
    if not local_subset(root):
        return {}
    return {"evaluation_scope": "published-subset", "official_full_pool": False,
            "rank_basis": "one-based position among published opponents"}

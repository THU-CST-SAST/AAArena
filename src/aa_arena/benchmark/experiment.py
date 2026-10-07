"""Immutable controller experiments; absent configuration preserves baseline behavior.

Binary feedback means win versus non-win: draws (and losses) both map to False.
The seed controls opponent sampling and clone replicates, never ordinary match seeds.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from aa_arena.io import canonical_hash


@dataclass(frozen=True)
class ExperimentConfig:
    opponent_policy: str = "model"
    feedback: str = "detailed"
    initial_rank: int = 25
    seed: int = 20260915
    clone_rank: int | None = None
    # Controller-only reproducibility for seat-level Saiblo seeds (default preserves legacy runs).
    match_base_seed: int | None = None
    # When set, every small_match must request exactly this many opponent IDs.
    fixed_small_batch: int | None = None

    def __post_init__(self) -> None:
        if self.opponent_policy not in {
            "model",
            "ladder",
            "random",
            "top5",
            "top4",
            "clone",
            "offpolicy",
        }:
            raise ValueError("invalid opponent_policy")
        if self.feedback not in {"detailed", "binary"}:
            raise ValueError("feedback must be detailed or binary")
        if type(self.initial_rank) is not int or not 1 <= self.initial_rank <= 200:
            raise ValueError("initial_rank must be between 1 and 200")
        if type(self.seed) is not int or not 0 <= self.seed < 2**63:
            raise ValueError("seed must be a non-negative 63-bit integer")
        if self.match_base_seed is not None and (
            type(self.match_base_seed) is not int or not 0 <= self.match_base_seed < 2**63
        ):
            raise ValueError("match_base_seed must be a non-negative 63-bit integer")
        if self.fixed_small_batch is not None:
            if type(self.fixed_small_batch) is not int or not 1 <= self.fixed_small_batch <= 8:
                raise ValueError("fixed_small_batch must be between 1 and 8")
        if self.is_clone:
            if type(self.clone_rank) is not int or self.clone_rank not in {5, 15, 25, 35}:
                raise ValueError("clone_rank must be one of 5, 15, 25, 35")
        elif self.clone_rank is not None:
            raise ValueError("clone_rank is only valid for clone")

    @property
    def is_clone(self) -> bool:
        return self.opponent_policy == "clone"

    @property
    def is_offpolicy(self) -> bool:
        return self.opponent_policy == "offpolicy"

    @property
    def binary_feedback(self) -> bool:
        return self.feedback == "binary"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public_dict(self) -> dict[str, Any]:
        """Model-visible policy metadata; sampling seeds stay controller-private."""
        hidden = {"seed", "match_base_seed"}
        return {key: value for key, value in self.as_dict().items() if key not in hidden}

    def resolved_match_base_seed(self) -> int:
        return 20260831 if self.match_base_seed is None else self.match_base_seed

    def small_batch_bounds(self) -> tuple[int, int]:
        if self.fixed_small_batch is not None:
            return self.fixed_small_batch, self.fixed_small_batch
        return 1, 8

    @property
    def fingerprint(self) -> str:
        return canonical_hash(self.as_dict())

    def validate_budgets(self, small: int, large: int) -> None:
        if self.is_clone and (small, large) != (32, 0):
            raise ValueError("clone requires small_budget=32 and large_budget=0")
        if self.is_offpolicy and (small, large) != (128, 16):
            raise ValueError("offpolicy requires small_budget=128 (trajectory views) and large_budget=16")
        if not self.is_clone and not self.is_offpolicy and large < 1:
            raise ValueError("non-clone experiments require a positive large budget")

    @classmethod
    def load(cls, path: Path) -> ExperimentConfig:
        path = Path(path)
        if not path.exists():
            return cls()
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("experiment.json must be an object")
        unknown = set(value) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"unknown experiment fields: {sorted(unknown)}")
        return cls(**value)

    def freeze(self, path: Path) -> None:
        """Seal normalized configuration before initializing or resuming a ledger.

        The controller-only seal also detects deletion of a configured experiment.
        Existing baseline runs can acquire the default seal without changing behavior.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        seal = path.with_name("experiment.frozen.json")
        payload = json.dumps(self.as_dict(), sort_keys=True, indent=2) + "\n"
        try:
            descriptor = os.open(seal, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
        except FileExistsError:
            if ExperimentConfig.load(seal) != self:
                raise ValueError("immutable experiment configuration changed")
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        if not path.exists():
            with path.open("x", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        path.chmod(0o400)


def binary_small_result(result: dict[str, Any]) -> dict[str, Any]:
    """Allowlist small feedback, idempotently, before *any* public persistence.

    Do not copy arbitrary nested result keys: diagnostic/replay/score fields can
    contain private match evidence. Controller bookkeeping is added by the service.
    """
    return {
        "kind": "small",
        "opponents": list(result.get("opponents", [])),
        "seats": [
            {
                "opponent_id": row["opponent_id"],
                "candidate_roles": list(row["candidate_roles"]),
                "win": row.get("win") is True if "win" in row else row.get("outcome") == "win",
            }
            for row in result.get("seats", [])
        ],
    }

"""Conservative local match concurrency, including referee/host headroom."""

import os
from pathlib import Path


def default_match_capacity(*, topology_root=None, cpu_ids=None):
    """Count available physical cores, not SMT threads or individual players.

    A match can run multiple player processes and a referee. This is a starting
    limit for local testing, not a guarantee against machine-wide contention.
    """
    root = Path(topology_root or "/sys/devices/system/cpu")
    if cpu_ids is None:
        try:
            cpu_ids = os.sched_getaffinity(0)
        except (AttributeError, OSError):
            cpu_ids = range(os.cpu_count() or 1)
    cpu_ids = tuple(cpu_ids)
    cores = set()
    try:
        for cpu in cpu_ids:
            topology = root / f"cpu{cpu}" / "topology"
            cores.add((int((topology / "physical_package_id").read_text()),
                       int((topology / "core_id").read_text())))
        count = len(cores)
    except (OSError, ValueError):
        # Unknown topology: avoid treating every logical CPU as a full core.
        count = max(1, len(cpu_ids) // 2)
    # Each admitted match owns a referee and multiple player processes. A
    # physical-core count is not a match count: even modest interference can
    # push a valid decision near its fixed wall-clock deadline over the limit.
    headroom = max(1, (count + 3) // 4)
    return max(1, min(32, (count - headroom) // 2))

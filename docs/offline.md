# Local off-policy observations

Create a frozen replay catalog using the published human-player subset. Catalog generation makes no model calls. It plays all pairs of the selected opponents and can be expensive; use a separate output directory for each pool and preserve its manifests.

```bash
mkdir -p catalogs/pacman
python - <<'PYTHON'
from pathlib import Path
from aa_arena.benchmark.matches import freeze_opponents
from aa_arena.elo.model import repository_root
freeze_opponents("pacman", repository_root(), Path("catalogs/pacman/pool.json"))
PYTHON
export OFFPOLICY_CATALOG_WORKERS=8
python scripts/offline/make_pool_dense_catalog.py --game pacman \
  --pool catalogs/pacman/pool.json --output catalogs/pacman --match-base-seed 42
python scripts/run_experiments.py plan --suite offpolicy --profiles default \
  --games pacman --output plans/offpolicy.json
python scripts/run_experiments.py run --plan plans/offpolicy.json \
  --catalog-root catalogs --jobs 1 --workers 8 --seat-capacity 16
```

Use the corresponding game identifier and directory for any of the 12 games. The controller verifies catalog hashes and exposes 128 trajectory views to the learner. The learner cannot initiate small matches; its 16 large evaluations run remotely against the complete frozen pool. The coding agent has no access to catalog-generation source programs through its resource bundle. The hosted replay catalog is not provisioned. Supply a frozen catalog explicitly; report whether its human trajectories come from the published subset or complete pool.

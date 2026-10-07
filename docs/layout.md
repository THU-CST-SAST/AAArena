# Package layout

| Path | Contents |
| --- | --- |
| `assets/*.tar.gz` | 12 games, referees, SDKs and permitted even-rank practice players |
| `assets/manifest.json` | Archive digests and sizes |
| `games/GAME/players/publication.json` | Exact published player IDs and frozen Elo ranks |
| `results/elo/` | Complete fixed evaluation leaderboard metadata |
| `results/availability/` | Evaluation-pool availability metadata; not source packages |
| `configs/distribution.json` | Published-subset local evaluation mode |
| `src/aa_arena/` | Local game runtime, coding-agent harnesses and budgeted local evaluation |
| `scripts/practice.py` | Local practice against published programs, without official Elo |
| `scripts/run_experiments.py` | Main, continuation and ablation experiment plans |
| `validation/` | Release verification evidence |

The release's player-code rule is `rank > 8 and rank % 2 == 0`, using the experiment's frozen measured Elo order. The complete evaluation service is not available in this release. Local models receive the configured public resources and permitted match feedback. The binary-feedback arm exposes no dense replay artifacts. Off-policy catalogs provide observations without player source code.

Game-local provenance and attribution identify resource origins. Preserve the applicable license and attribution files. Runtime environments, complete private pools, paid experiment transcripts, credentials and server state are not public distribution inputs.

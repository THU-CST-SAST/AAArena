# Validation scope

The public asset audit checks all 12 archives, file checksums and the exact 909 permitted human strategies against the frozen measured Elo tables. The installer rejects extra opponent packages. No private strategies, provider credentials or evaluation tokens are distributed.

The remote SDK matrix runs all 12 games through the authenticated HTTPS API. Each game exercises detailed and binary small-match feedback, actual game rounds, replay availability or exclusion, and exact local budget accounting. Controller tests additionally verify complete-pool metadata, immutable IDs, duplicate submission handling, quota and experiment constraints, continuation, receipt integrity and unavailable-service behavior.

Runtime validation uses x86-64 Linux, official native Codex 0.147.0, official Claude Code 2.1.231 and Claude Agent SDK 0.2.162. Automatic-compaction acceptance uses reduced test windows to exercise native threshold-triggered compaction without a full paper experiment. Formal defaults remain a 200,000-token Codex threshold and a 200,000-token Claude auto-compact window with its native safety margin. Manual compaction and deterministic model fixtures are separate checks.

See `release-verification.json` for measured acceptance status. Historical opponent forfeits are distinct from service failures. The tests do not promise identical stochastic Elo values across repetitions or compatibility with every external model provider. The hosted off-policy replay catalog is not provisioned.

## Release acceptance requirements

All three conditions must pass:

1. Every unchanged player with successful execution in the agentlab historical baseline executes successfully on the hosted evaluator. Discrepancies require comparison of original source hashes, entrypoints, SDK protocols, dependencies and resource limits. Paired diagnostics use the same candidate, seat and seed. A failure on both hosts does not supersede a recorded historical success without reconciling the configuration and execution path.
2. A local client submits policy code for real hosted small and full-pool large evaluations. Feedback, budgets, strategy hashes, receipts and actual match artifacts must agree.
3. A clean installation of the release runs all twelve games with the published opponent subset locally, supports local agent iteration and invokes hosted evaluations using the documented setup.

API completion, an Elo value, matching runtime hashes or accurate error counters alone do not establish player runtime parity. No opponent may be deleted, replaced or edited to satisfy acceptance. The hosted full-pool off-policy catalog is not provisioned. Repository visibility requires the owner's decision.

## Complete-pool interface matrix

Release acceptance is pending. The frozen-runtime HTTPS matrix and transport-level error audit are required release gates. Rollman, AntWar and LostSpace raw logs contain in-game error events absent from API counters. Error-reporting regression tests cover two-player and four-player assignments; the pinned-backend runtime passes all 12 detailed/binary small-match checks. Its eight Saiblo complete-pool HTTPS sweeps are in progress. Original results and receipts remain immutable.

The interface matrix contains 3,776 matches against all 1,920 frozen human opponents. These are SDK candidate checks, not model benchmark scores. Error columns contain adapter-reported counters and require transport-level verification. Zero counters are not evidence of zero in-game errors. Root-cause attribution requires run-specific diagnosis; every opponent remains in the evaluation pool.

| Game | Opponents | Matches | Candidate reported errors | Opponent reported errors | Interface check |
|---|---:|---:|---:|---:|---|
| rollman | 64 | 64 | 0 | 0 | Pass |
| pacman | 44 | 88 | 0 | 4 | Pass |
| antwar | 114 | 228 | 0 | 0 | Pass |
| aquawar | 170 | 340 | 0 | 47 | Pass |
| generals | 197 | 394 | 0 | 0 | Pass |
| lostspace | 111 | 222 | 0 | 0 | Pass |
| miracle | 253 | 506 | 0 | 67 | Pass |
| dorado | 323 | 646 | 0 | 48 | Pass |
| monecraft | 112 | 224 | 0 | 33 | Pass |
| lota | 200 | 400 | 0 | 25 | Pass |
| snakego | 141 | 282 | 1 | 17 | Pass |
| antwar2 | 191 | 382 | 0 | 0 | Pass |

SnakeGo's sample candidate has one official illegal-action exception. The API reports that policy failure correctly. Opponent errors include illegal actions, segmentation faults, resource-limit violations and timeouts; acceptance does not require every historical policy to avoid forfeits. Miracle's full-pool run contains no missing-dependency errors. Dorado's 646 replay archives pass integrity checks; optional legacy postprocessing commands emit diagnostics, while the Python adapters provide valid normalized results and replay data.

The 12-game dense/binary small matrix and both live native harness checks use the production HTTPS endpoint. Pacman, MoneCraft, LOTA and Dorado full-pool checks use an authenticated HTTP validation instance on the same host with isolated GCC 14.2 player execution. Other full-pool checks use production HTTPS. Pool membership, candidate hashes, receipts, budgets and actual match artifacts are verified. The hosted full-pool off-policy replay catalog is outside this acceptance scope and is not provisioned.

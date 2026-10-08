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

## Complete-pool execution coverage

Release acceptance is pending historical player runtime parity and the final local live-game regression. The HTTPS checks verify all frozen opponents, actual match artifacts, immutable submissions, candidate hashes, receipts and budgets. These are SDK candidate checks, not model benchmark scores. A counted game error requires case-specific diagnosis; it does not certify a player defect.

The Python-referee profile uses the pinned Python 3.14.6 backend and Debian reference runtime. The native-referee profile uses GCC 14.2 and the reference runtime; its full-pool records are identified separately. No Elo scores are combined across profiles. The 12-game detailed/binary small matrix uses the deployed HTTPS service. Error counts below are per-match and may overlap between candidate and opponent.

| Game | Referee profile | Opponents | Matches | Candidate errors | Opponent errors | Infrastructure retries |
|---|---|---:|---:|---:|---:|---:|
| rollman | Python 3.14.6 | 64 | 64 | 0 | 4 | 0 |
| pacman | Native C++ | 44 | 88 | 0 | 3 | 0 |
| antwar | Python 3.14.6 | 114 | 228 | 6 | 12 | 0 |
| aquawar | Python 3.14.6 | 170 | 340 | 0 | 49 | 0 |
| generals | Python 3.14.6 | 197 | 394 | 0 | 34 | 0 |
| lostspace | Python 3.14.6 | 111 | 222 | 8 | 83 | 0 |
| miracle | Python 3.14.6 | 253 | 506 | 0 | 67 | 0 |
| dorado | Native C++ | 323 | 646 | 0 | 47 | 0 |
| monecraft | Native C++ | 112 | 224 | 0 | 31 | 0 |
| lota | Native C++ | 200 | 400 | 0 | 23 | 0 |
| snakego | Python 3.14.6 | 141 | 282 | 3 | 18 | 0 |
| antwar2 | Python 3.14.6 | 191 | 382 | 7 | 11 | 0 |

All eight Python-referee pools pass independent counter attribution checks. AquaWar includes native game-rule errors recorded in replay data as well as transport errors. Four-role attribution is verified for LostSpace. Every original opponent remains in the pool. These checks do not substitute for reconciling hosted failures with agentlab historical successful execution.

The hosted full-pool off-policy replay catalog is not provisioned.

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

Release acceptance is pending historical player runtime parity. A clean installation passes all twelve local live-game checks, and 96 source, script and asset hashes match the tested runtime. The HTTPS checks verify all frozen opponents, actual match artifacts, immutable submissions, candidate hashes, receipts and budgets. These are SDK candidate checks, not model benchmark scores. A counted game error requires case-specific diagnosis; it does not certify a player defect.

The table records the Python-referee execution profile at `13bb7f4`, using the pinned Python 3.14.6 backend and Debian reference runtime. The native-referee profile uses GCC 14.2 and the reference runtime; its full-pool records are identified separately. No Elo scores are combined across profiles. The 12-game detailed/binary small matrix uses the deployed HTTPS service. Error counts below are per-match and may overlap between candidate and opponent.

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

## Saiblo deadline validation

The deployed runtime `35b6529` resolves round deadlines using frame receive timestamps. Twenty-two focused tests cover delayed dispatch, state transitions, genuine late replies and SnakeGo multi-action states. Seventeen matched cases on each reference host verify unchanged source hashes without infrastructure exceptions; historical player runtime parity remains pending. The twelve-game HTTPS detailed/binary small matrix and twelve-game local matrix pass for this runtime. The eight-game Python-referee full-pool matrix is in progress. Seven-game paired diagnostics use frozen paper candidates and their recorded experiment seeds. Round limits and human strategy sources are unchanged.

## Concurrency validation

The hosted evaluator has 8 physical cores and 16 logical CPUs. Its shared admission capacity is 3 complete matches, with priority for 1 small match. Active matches drain before scheduling configuration changes. Five matched AntWar cases complete without transport errors at a capacity of 6; all five have timeout evidence under the 16-match configuration. Seven focused tests verify physical-core capacity selection and shared admission. Broader historical-policy comparisons remain required. The in-progress complete-pool matrix records a scheduling transition and serves as diagnostic execution evidence, not a uniform-concurrency model benchmark.

The deployed deadline runtime has complete-pool execution and receipt-integrity evidence for Rollman (64 matches), AntWar (228), AquaWar (340), Generals (394) and LostSpace (222). The checks cover immutable candidate snapshots, complete opponent pools, budgets, result hashes and actual match artifacts. Three Python-referee pools remain in progress. Scheduling transitions are part of the diagnostic provenance; these results do not establish historical player runtime parity.

A timeout-sensitive Generals policy passes three isolated runs and three runs at a shared capacity of 3, each completing 501 rounds with identical original source hashes and the recorded seed. The isolated runs require 950–962 ms for a decision with a 1,000 ms limit. This narrow margin requires continued checks under load; no decision limit is extended. The corresponding reference decision takes 726 ms.

The seven-game paper-candidate diagnostic covers 318 assignments on each host, without infrastructure exceptions. An additional historical AntWar2 snapshot supplies sixteen paired assignments. A Rollman comparison with explicit seed handling has an identical initial map and the same bundled-SDK JSON parsing failure at round 352 on both hosts. Its unseeded historical backend does not establish identical input from the requested seed alone. A LostSpace candidate shutdown error occurs after its successful escape, and its raw transport evidence remains available. These are individual diagnostic findings, not blanket certification of historical player runtime parity.

## SDK and effective-seed diagnostics

A 510-action Rollman replay matches the referee simulator exactly. The bundled historical SDK diverges at level 2, round 93 and fails to recognize the referee's level transition at round 352. The SDK's collision/invulnerability rules differ from the referee. Player sources remain unchanged, and compatibility acceptance is pending.

The four native games have a paired diagnostic covering 55 original opponents and 110 assignments per host. The reference host has 30 assignments without official engine errors and 80 with engine-reported errors; hosted comparisons are in progress. These are diagnostic cases, not benchmark scores. A native Pacman case reproduces the reference's entire 64-round replay on the hosted evaluator after reconstruction of its actual wall-clock seed. A reported seed alone cannot establish equivalent inputs for a backend that seeds itself from the clock.

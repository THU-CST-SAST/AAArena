# Validation scope

The public asset audit checks all 12 archives, file checksums and the exact 909 permitted human strategies against the frozen measured Elo tables. The installer rejects extra opponent packages. No private strategies, provider credentials or evaluation tokens are distributed.

The remote SDK matrix runs all 12 games through the authenticated HTTPS API. Each game exercises detailed and binary small-match feedback, actual game rounds, replay availability or exclusion, and exact local budget accounting. Controller tests additionally verify complete-pool metadata, immutable IDs, duplicate submission handling, quota and experiment constraints, continuation, receipt integrity and unavailable-service behavior.

Runtime validation uses x86-64 Linux, official native Codex 0.147.0, official Claude Code 2.1.231 and Claude Agent SDK 0.2.162. Automatic-compaction acceptance uses reduced test windows to exercise native threshold-triggered compaction without a full paper experiment. Formal defaults remain a 200,000-token Codex threshold and a 200,000-token Claude auto-compact window with its native safety margin. Manual compaction and deterministic model fixtures are separate checks.

See `release-verification.json` for measured acceptance status. Historical opponent forfeits are distinct from service failures. The tests do not promise identical stochastic Elo values across repetitions or compatibility with every external model provider. The hosted off-policy replay catalog is not provisioned.

## Release acceptance requirements

All three conditions must pass:

1. Original strategies and decision limits are preserved, and reference and hosted execution have consistent behavior. Paired diagnostics verify source hashes, entrypoints, SDK protocols, dependencies, limits and effective inputs. Confirmed original-policy failures are reported faithfully and may satisfy behavioral parity. Historical successful runs remain evidence for configuration and execution-path checks. Unexplained differences and infrastructure or SDK defects require resolution.
2. A local client submits policy code for real hosted small and full-pool large evaluations. Feedback, budgets, strategy hashes, receipts and actual match artifacts must agree.
3. A clean installation of the release runs all twelve games with the published opponent subset locally, supports local agent iteration and invokes hosted evaluations using the documented setup.

API completion, an Elo value, matching runtime hashes or accurate error counters alone do not establish player runtime parity. No opponent may be deleted, replaced or edited to satisfy acceptance. The hosted full-pool off-policy catalog is not provisioned. Repository visibility requires the owner's decision.

## Complete-pool execution coverage

Release acceptance is pending complete-pool behavioral parity. A clean installation passes all twelve local live-game checks, and 96 source, script and asset hashes match the tested runtime. The HTTPS checks verify all frozen opponents, actual match artifacts, immutable submissions, candidate hashes, receipts and budgets. These are SDK candidate checks, not model benchmark scores. A counted game error requires case-specific diagnosis; it does not certify a player defect.

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

All eight Python-referee pools have transport-counter reconciliation evidence. The counts follow their recorded runtime event semantics and are not certified player-fault classifications. AquaWar includes native game-rule errors recorded in replay data as well as transport errors. Four-role attribution is verified for LostSpace. Every original opponent remains in the pool. These checks do not substitute for reconciling hosted failures with agentlab historical successful execution.

The hosted full-pool off-policy replay catalog is not provisioned.

## Saiblo deadline validation

Runtime `35b6529` resolves round deadlines using frame receive timestamps. Twenty-two focused tests cover delayed dispatch, state transitions, genuine late replies and SnakeGo multi-action states. Seventeen matched cases on each reference host verify unchanged source hashes without infrastructure exceptions; historical player runtime parity remains pending. The twelve-game HTTPS detailed/binary small matrix and twelve-game local matrix pass for this runtime. The eight-game Python-referee full-pool matrix has eight completed execution-integrity audits covering 2,418 matches. Seven-game paired diagnostics use frozen paper candidates and their recorded experiment seeds. Round limits and human strategy sources are unchanged.

## Concurrency validation

The hosted evaluator has 8 physical cores and 16 logical CPUs. Its shared admission capacity is 3 complete matches, with priority for 1 small match. Active matches drain before scheduling configuration changes. Five matched AntWar cases complete without transport errors at a capacity of 6; all five have timeout evidence under the 16-match configuration. Seven focused tests verify physical-core capacity selection and shared admission. Broader historical-policy comparisons remain required. The complete-pool diagnostic matrix records a scheduling transition and serves as diagnostic execution evidence, not a uniform-concurrency model benchmark.

The deployed deadline runtime has complete-pool execution and receipt-integrity evidence for Rollman (64 matches), AntWar (228), AquaWar (340), Generals (394), LostSpace (222), SnakeGo (282), Miracle (506) and AntWar2 (382), totaling 2,418 matches. The checks cover immutable candidate snapshots, complete opponent pools, budgets, result hashes and actual match artifacts. Scheduling transitions are part of the diagnostic provenance; these results do not establish historical player runtime parity.

A timeout-sensitive Generals policy passes three isolated runs and three runs at a shared capacity of 3, each completing 501 rounds with identical original source hashes and the recorded seed. The isolated runs require 950–962 ms for a decision with a 1,000 ms limit. This narrow margin requires continued checks under load; no decision limit is extended. The corresponding reference decision takes 726 ms.

The seven-game paper-candidate diagnostic covers 318 assignments on each host, without infrastructure exceptions. An additional historical AntWar2 snapshot supplies sixteen paired assignments. A Rollman comparison with explicit seed handling has an identical initial map and the same bundled-SDK JSON parsing failure at round 352 on both hosts. Its unseeded historical backend does not establish identical input from the requested seed alone. A LostSpace candidate shutdown error occurs after its successful escape, and its raw transport evidence remains available. These are individual diagnostic findings, not blanket certification of historical player runtime parity.

## SDK and effective-seed diagnostics

A 510-action Rollman replay matches the referee simulator exactly. The bundled historical SDK diverges at level 2, round 93 and fails to recognize the referee's level transition at round 352. The SDK's collision/invulnerability rules differ from the referee. Player sources remain unchanged, and compatibility acceptance is pending.

The four native games have a paired diagnostic covering 55 original opponents and 110 assignments per host. The reference diagnostics contain 6 assignments without official engine errors, 78 with engine-reported errors, and 26 LOTA assignments excluded from parity assessment because their launch configuration ends at round zero. All 110 hosted diagnostic assignments are complete: 19 without engine errors and 91 with engine-reported errors, with no infrastructure exceptions. These are diagnostic cases, not benchmark scores. A native Pacman case reproduces the reference's entire 64-round replay on the hosted evaluator after reconstruction of its actual wall-clock seed. A reported seed alone cannot establish equivalent inputs for a backend that seeds itself from the clock.

A private Rollman referee fixture supplies all three recorded initial maps and spawn positions from a successful reference case. The unchanged hosted players complete level 3 at round 300 without transport errors, matching the reference completion point. Each replay contains 725 records; trajectories differ from record 147, and the original player includes unseeded random action selection. This proves execution of this successful case on the hosted stack; it does not certify identical trajectories or universal SDK compatibility. Scores and decision limits are not overridden.

LOTA requires two competitors and a neutral controller. The 26 excluded reference cases use two loaders and `map.txt 1`; the hosted server uses three loaders and `map.txt 0` with competitor assignments `0 1`. A 79-file source comparison verifies the same hosted runtime and LOTA backend on the reference host for a separate full-game regression. Historical source and candidate hashes alone do not certify the excluded runs as successful matches.

The equivalent LOTA reference regression completes 26 assignments: 5 without engine errors and 21 with engine-reported errors, with no infrastructure exceptions. One reference-success/hosted-timeout comparison involves an original player that calls `srand(time(0))` each round; its uncontrolled trajectories diverge at round 2. A private loader fixture holds wall-clock time constant while preserving CPU and monotonic clocks, original player sources and the 100 ms decision limit. Both hosts produce identical complete replays containing 385 round records. Maximum measured opponent decision CPU times are 11.70 ms and 11.89 ms. The loader binaries match, and the time override is verified on both hosts. The hosted service has no such clock override; universal historical parity remains pending.

Historical provenance is verified against the origin server recorded in the frozen export. The archived LOTA champion ledger passes its recorded SHA-256 check and contains 400 games with 33 reported opponent errors. Twelve of the thirteen diagnostic targets have reported errors in the sixteen historical large evaluations; the clock-dependent target has errors in nine evaluations. Four normalized feedback files are recovered from the original snapshot chain with size and SHA-256 verification. These records establish historical error occurrence, not its underlying cause. A same-path directory on a different host is not a substitute for the recorded experiment origin.

AntWar2 paper-candidate diagnostics cover sixteen reference assignments using the recorded experiment seed and immutable candidate. Ten reference assignments contain transport error records despite a `complete` result status. Events preceding `GameOver` require lifecycle analysis because a player may receive terminal information first. A player requires the absent `tianshou` package; an isolated dependency installation passes its data-container import check but exposes numerical-library thread creation failure inside the player sandbox. A private environment with the required dependencies and one-thread numerical-library settings completes both assignments at 210 and 206 rounds without transport errors. The player sources and decision limits match the diagnostic inputs. Forty-five focused tests pass; one platform-dependent test is skipped. The corresponding hosted diagnostics complete both assignments at 198 and 203 rounds, without transport errors or infrastructure exceptions. The authenticated service uses this dependency and thread configuration in profile `terminal-lostspace-v1`; authenticated match regression is in progress.

## Terminal lifecycle and dependency integrity

An idle player's closed transport is retained as an exit event. A subsequent referee request for that player's reply makes it a runtime error; a terminal result does not. AntWar2's official `RE`, `TLE`, `OLE` and `IA` statuses attribute runtime failures and illegal actions to their reported seats, independently of transport EOF ordering. Terminal scores remain authoritative. Twenty-four transport tests and twenty-nine attribution tests pass. Hosted original-strategy regression completes six assignments with byte-identical full replays, scores and official terminal states against the recorded baseline. Three illegal-action forfeits and two timeouts retain their correct failed seats; one normal match has no failed seat. Authenticated-service match regression for `terminal-lostspace-v1` is in progress.

Both candidate player environments preserve all 38,056 original non-cache files. The 669 added payload files match across hosts; installer metadata differs only in three explicit-install markers and their record entries. The fresh public-package controller and player environments install successfully and pass public resource and contract checks plus all twelve local games with the bounded numerical-library configuration. The terminal lifecycle runtime passes all twelve local games in that clean environment, covering 52 live matches and 22 runnable starters, with 259 packaged-file hashes verified. These component checks do not establish the three release acceptance gates.

## Deterministic player build resources

The public LostSpace player at measured-Elo rank 98 requires five NumPy map tables generated by its bundled `make_map.make_map()` function. These 9,604 bytes have identical hashes in both environments; all original strategy source files retain their hashes. Two assignments per host reach round 100 without missing-asset failures. One assignment per host is error-free. The other assignment retains a candidate shutdown error, and one reference opponent has an exit-zero transport event requiring lifecycle analysis. The package includes these deterministic build outputs and `assets/player-builds.json` provenance. A clean LostSpace resource installation verifies all 430 files and passes its local live-game contract. The resources are deployed in `terminal-lostspace-v1`; authenticated match regression is in progress.

## LostSpace inactive-player protocol

LostSpace requests replies only from players whose status permits an action (`Alive` or `WaitForEsacape`). Escaped, eliminated, skipped and respawning players receive the same notifications without a reply requirement; respawning players can respond once alive. The protocol follows the referee’s turn-processing conditions and retains action deadlines for active players. Four paired assignments with unchanged candidate and human strategy sources reach round 100 with no in-game transport errors. Twenty-four focused tests and the public local live-game contract pass. Raw interpreter-shutdown diagnostics after escape remain available; full replay equality is not claimed for all assignments. The backend is deployed in `terminal-lostspace-v1`; authenticated match regression is in progress.

## Optional player weights

One unchanged AntWar2 policy catches unavailable neural-network weights and selects its own bundled heuristic. Six hosted assignments across three recorded runtime profiles finish with official `OK` states for both players and no transport errors. The weight-file message is an explicit fallback diagnostic, not an unhandled exception. Original stderr and event hashes are retained. This evidence establishes successful execution of the policy’s built-in fallback; it does not establish availability or equivalence of a historical trained checkpoint.

## Public AntWar2 SDK refund contract

The public Python SDK forwards an optional tower state when calculating downgrade refunds. The interface supports both the full-health refund and the health-scaled refund defined by its engine. Thirteen SDK and backend concurrency tests pass, including eight refund cases across two tower levels and four health values. The local AntWar2 live-game contract passes with the public package. Original player packages and their bundled SDK files retain their bytes; this public SDK contract does not certify those packages against all historical inputs.


## AntWar2 bundled SDK compatibility

An unchanged player invokes a two-argument downgrade-refund method through a bundled Python SDK adapter that accepts one argument. Paired diagnostic executions verify the same candidate and player source hashes and the same experiment seed, using the same release runtime and unlimited CPU policy. Both hosts reproduce the adapter TypeError in the same policy call path; the trajectories and failure rounds differ. This establishes an SDK interface defect for those inputs, but does not establish historical-success parity. The public SDK accepts the optional tower argument and forwards it to the engine, with thirteen focused tests and a local live-game contract passing. Original human player packages retain their hashes.


## Hosted deployment acceptance

Profile `terminal-lostspace-v1` has sixteen verified deployed file hashes and an authenticated HTTPS health response listing all twelve games. It includes the bounded numerical-library configuration, required player dependencies, terminal-exit handling, deterministic LostSpace assets and inactive-player reply handling. Original human strategy sources retain their hashes. The twelve-game detailed/binary small-match matrix passes with 24 authenticated submissions and 46 actual matches. An independent audit verifies candidate snapshot hashes, request/result/pool hashes, budgets, receipts, local replay bytes and feedback boundaries. Binary feedback exposes no dense artifacts; these small matches have no in-game transport errors. LostSpace and AntWar2 complete-pool regressions are running under this profile with distinct immutable submission records. Their acceptance is pending. Full-pool off-policy trajectories are unavailable.


## Reply ownership and turn initialization

Buffered replies belong to the input that produced them. A new state with input for the same player supersedes its buffered response; listen-only transitions preserve early binary-SDK replies. LostSpace accepts a reply only after the corresponding player's formal round-begin notification, including turns following respawn. Notifications, action deadlines, game rules and original player code retain their contracts.

Eight paired LostSpace matches cover two unchanged original opponents and both seat assignments on each host. Every match reaches 100 rounds with no in-game transport errors. Forty-five focused protocol and SDK tests pass. The clean public environment passes all twelve local game contracts, covering 52 actual matches and 22 runnable starters. Hosted deployment and complete-pool acceptance for this runtime are pending. These bounded checks do not certify universal historical-player parity.


## LostSpace variant build resources

Four original player variants share a byte-identical deterministic map generator. Their packages require five generated NumPy tables each. The public measured-Elo ranks 96 and 98 include these tables and their build provenance; the other variants remain server-only. Original strategy files retain their hashes. Three variant packages have twelve paired matches across the two hosts, all reaching 100 rounds without in-game transport errors. The public LostSpace local contract passes. Hosted deployment acceptance is pending.

A broader reference-host diagnostic covers 39 additional unchanged opponents and 78 assignments under the turn-gate runtime. Fifteen assignments complete without game errors; 63 have errors requiring individual reconciliation. None contains the SDK message-format exception targeted by the transport regression. Missing generated tables, source-level attribute exceptions, timeouts and native crashes remain distinct findings; this diagnostic does not certify universal historical-player parity.

### Local evaluator error attribution

The public local evaluation entry point reports player crashes, timeouts and official forfeits as `game_error`, preserving the official winner, scores, rounds and replay. Errors after the terminal game result do not count as player failures. Validation comprises 16 focused tests, one live four-player failure case and five normal LostSpace contract matches. These checks do not certify full-pool historical runtime parity.


## Hosted terminal-profile verification

Profile `terminal-lostspace-v1` has a verified 12-game detailed/binary small matrix with 24 submissions and 46 actual matches. Its LostSpace full-pool submission covers all 111 opponents in 222 actual matches. Immutable snapshots, request and result hashes, complete-pool metadata, raw match artifacts and budget receipts agree. The experiment receipt records one small and one large evaluation used. The SDK candidate has no counted errors; 85 matches contain opponent errors requiring individual reconciliation. AntWar2 is running. Execution-integrity verification does not establish error-free player execution or release acceptance.

Two LostSpace native-crash diagnostics preserve original sources and match both the binary and the entire captured input stream across hosts. Sandboxed replay of each original binary stops at a compiler-generated illegal instruction where a non-void source function reaches its end without returning a value. The relevant sources match the historical revision; historical process traces remain necessary for full provenance reconciliation.

An AntWar2 original policy uses the Python backend explicitly. Uninstrumented paired matches with the same seed time out after 16 reference rounds and 9 hosted rounds; identical trajectories are not claimed for its wall-clock-budgeted search. An external reference-host sample records 8.004934 wall seconds and 8.003734 CPU seconds with no cgroup throttling. Hosted in-turn CPU samples are unavailable. The recorded 289-round historical screening result remains part of the unresolved comparison. Intrusively instrumented probes are excluded from acceptance conclusions.


## AntWar2 SDK compatibility

The evaluator supports the optional tower argument in a version-pinned Python SDK adapter. The adapter and engine must both match the verified source hashes. Runtime copies preserve all policy bytes and carry a manifest of the sole SDK change; unknown SDK versions retain their own implementation. Thirteen focused tests cover argument forwarding, immutable sources, concurrent publication, cache integrity and custom SDK preservation. Four original-opponent matches across the reference and hosted machines pass, and two matches pass through the automatic evaluator integration. The public Python and C++ starter contract passes five actual local matches. Hosted production deployment and authenticated full-pool regression for this compatibility profile remain pending.


## LostSpace material feedback and SDK dispatch

Successful material collection returns the acting player's inventory through the material-response interface. Failed actions omit inventory. The inventory serializer returns named counts for traps and tools. A version-pinned SDK adapter dispatches turns in both the alive and escape-waiting states, as required by the referee. The adapter preserves the original files and all strategy logic; its runtime manifest identifies the sole modified SDK function. Unknown sources retain their own dispatch implementation.

Nine focused tests cover response content, state-dependent dispatch, source preservation, concurrent preparation and cache tampering. Four paired original-opponent matches complete 100 rounds each without player errors. Two additional matches pass through the automatic evaluator integration. The public local contract passes five actual matches. Production deployment, authenticated full-pool regression and complete historical runtime parity remain pending.


## Hosted material and SDK profile

Profile `turn-gate-material-sdk-v5` has 30 verified deployed file hashes and authenticated HTTPS health coverage for all twelve games. The detailed/binary small matrix passes 24 submissions and 46 actual matches. An independent audit verifies immutable candidate manifests, request/result/pool hashes, budget receipts, feedback boundaries and local replay bytes. No preterminal transport errors occur in these small matches. The LostSpace and AntWar2 complete-pool regressions retain distinct immutable submission IDs and require completion and historical-player reconciliation. Complete release acceptance remains pending; the hosted full-pool off-policy catalogue is unavailable.


## Reproducible local setup

`scripts/setup_local.py` checks the Linux architecture, cgroup v2, user scopes, namespaces and exact compiler version before installing the controller and player dependencies. It verifies direct player-package pins, dependency consistency and isolated execution, then runs every public game contract. An activation file is available only after successful validation. The asset verifier rejects hard-linked files that cannot satisfy the build-isolation contract.

The setup validation uses an independently extracted release, fresh Python 3.14 controller and Python 3.10.14 player environments with all direct requirement pins and dependency consistency checked. All 271 release-manifest files match the independently extracted archive. All twelve local games pass, covering 52 actual matches and 22 runnable starters. Fifteen focused installer, platform-rejection, activation-failure and public-asset tests pass. This local evidence does not certify complete-pool historical player parity.

## Python entry points

The sandbox, systemd launcher, reference-runtime and Saiblo player-error modules each import successfully in a fresh interpreter. Eleven regression checks cover these entry points and evaluation status, player-error attribution and preservation of official game results. Hosted jobs use their pinned runtime profile.

## Frozen-input timeout parity

An original AntWar2 policy receives the same captured input on both hosts and produces byte-identical output. Its final decision requires 11.584 seconds on the reference host and 10.976 seconds on the hosted evaluator; CPU time closely matches wall time. Both exceed the preserved 10-second formal limit. Diagnostic profiling identifies nested rollout catalog construction as the computational cost. The formal matches retain their timeout outcomes. This case satisfies behavioral parity without changing the strategy or its deadline.

The `turn-gate-material-sdk-v5` LostSpace complete-pool submission covers 111 opponents and 222 actual matches. Independent audits verify candidate snapshots, request/result/pool hashes, budget receipts and replay artifacts. Candidate errors: 0; opponent-error matches: 55; infrastructure retries: 0. Paired behavioral verification covers all 222 matches. Both hosts report errors in the same 55 matches, with no reference infrastructure exception. Complete replays are byte-identical in 196 matches. Error labels agree in 205 matches; the other 17 have identical gameplay and failure states, with timeout versus stack-failure labels for the same recursive policy path. Corresponding native binaries match. Official errors remain unmodified. Stochastic trajectories and Elo are not required to be byte-identical.

## Conditional opponent failure diagnostics

Three conditional failures satisfy paired behavioral parity with original strategies and decision limits. AquaWar reaches the same `single_assert(int)` missing-return path and GCC `ud2` trap on both hosts with identical native binaries and captured input. SnakeGo reaches the same operation-history vector overflow in an SDK bundled by that particular human submission; an assertion build independently identifies the out-of-range access. The distributed public SDK does not contain that history insertion. Miracle contains a random branch that enters an infinite loop for two of ten possible initial draws. A diagnostic fixes the same random state and captured input on both hosts and observes the loop beyond the three-second deadline; formal evaluation retains its original randomness.

These diagnostics preserve the formal failed-match results. They certify the three identified execution paths, not all opponent failures. Complete release acceptance remains pending AntWar2 final-profile completion and the remaining cross-game error classifications. Private diagnostic inputs and human source code are not part of the distributed evidence.

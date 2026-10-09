# Release validation

Release 0.4.0 satisfies the three acceptance criteria below on the supported x86-64 Linux configuration. Original strategy files and decision limits are preserved. Confirmed policy and player-bundled SDK failures count as behavioral parity when paired evidence supports the diagnosis; their actual failure results remain visible.

| Acceptance criterion | Result | Evidence |
|---|---|---|
| Reference / hosted player behavior | Pass | Original source and configuration checks, paired game regressions and recorded-input diagnostics |
| Local policy submission to hosted evaluation | Pass | 12-game detailed/binary small matrix and complete-pool large evaluations; hashes, receipts, budgets and actual artifacts audited |
| Clean local package installation | Pass | Fresh controller and player environments; 12 games, 52 live matches, 22 starters; native agent iteration and hosted calls |

Acceptance covers the recorded matrix and its diagnosed discrepancies. It is not a claim that every original player succeeds on every input, or that stochastic outcomes and wall-clock timing are identical on different hardware. Repository visibility is an owner decision.

## Runtime and resource scope

The tested runtime uses Debian 13 x86-64, glibc 2.41, GCC/G++ 14.2, controller Python 3.14.6 and player Python 3.10.14. The reference system, player dependencies and backend dependencies have verified manifests. The hosted machine admits three complete matches concurrently and reserves one slot for small matches; original per-game decision limits apply. Other platforms require a compatible Linux machine or virtual machine.

The public release contains 909 permitted human strategies: measured-Elo ranks greater than eight with even rank numbers. The hosted service retains the full 1,920-player inventory. Original player source packages are retained, including their bundled SDKs. Hash-scoped runtime SDK adapters and deterministic build assets have dedicated tests. Public SDKs, rule files, replay tooling and evaluators are included.

## Hosted execution

The 12-game small-match matrix has 24 authenticated submissions and 46 actual matches. Detailed feedback includes validated replays; binary feedback excludes dense artifacts. The AntWar2 public protocol regression has two authenticated submissions and four additional matches, with independent receipt, budget, snapshot and artifact checks.

The complete-pool matrix contains 3,776 actual matches. Each row retains its execution profile and immutable records. These are SDK-candidate interface checks, not model leaderboard scores; Elo values from different profiles are not combined. Error counters are observations, not independent proof of a policy defect.

| Game | Execution profile | Full-pool players | Actual matches | Candidate errors | Opponent errors | Infrastructure retries |
|---|---|---:|---:|---:|---:|---:|
| rollman | `pinned-python-backend` | 64 | 64 | 0 | 4 | 0 |
| pacman | `reference-native-cpp` | 44 | 88 | 0 | 3 | 0 |
| antwar | `pinned-python-backend` | 114 | 228 | 6 | 12 | 0 |
| aquawar | `pinned-python-backend` | 170 | 340 | 0 | 49 | 0 |
| generals | `pinned-python-backend` | 197 | 394 | 0 | 34 | 0 |
| lostspace | `turn-gate-material-sdk-v5` | 111 | 222 | 0 | 55 | 0 |
| miracle | `pinned-python-backend` | 253 | 506 | 0 | 67 | 0 |
| dorado | `reference-native-cpp` | 323 | 646 | 0 | 47 | 0 |
| monecraft | `reference-native-cpp` | 112 | 224 | 0 | 31 | 0 |
| lota | `reference-native-cpp` | 200 | 400 | 0 | 23 | 0 |
| snakego | `pinned-python-backend` | 141 | 282 | 3 | 18 | 0 |
| antwar2 | `turn-gate-material-sdk-v5` | 191 | 382 | 0 | 9 | 0 |

AntWar2 and LostSpace use `turn-gate-material-sdk-v5`: 382 and 222 matches, respectively. Both have zero candidate errors and zero infrastructure retries. AntWar2 retains nine opponent-error matches (two timeouts and seven rule errors); LostSpace retains 55 opponent-error matches. The source, request, result and full-pool hashes, receipts and budgets agree with the client records.

## Behavioral findings

| Case | Paired evidence and classification |
|---|---|
| AntWar2 computation | Identical frozen input and output; original policy takes 11.58 s on the reference host and 10.98 s on the hosted machine, exceeding the unchanged 10 s limit. |
| AntWar2 original SDK serialization | Six rule-error matches reproduce on both hosts. Python 3.10 enum names are invalid numeric protocol fields. The public SDK serializes explicit integers; both Python versions and real matches pass. |
| AntWar2 original SDK economy | With 47 coins and three basic towers, the bundled formula predicts a 54-coin refund; the official formula grants 40. Both original SDK copies accept the unaffordable 90-coin action. The public SDK uses the official formula. |
| LostSpace | 222 paired assignments across 111 original packages; 196 entire replays match. All 55 error-bearing assignments agree. Seventeen timeout-versus-stack-failure labels have the same binary, failure path and gameplay apart from the error label; original labels are retained. |
| SnakeGo | Five duplex input/output traces reproduce every recorded reply and the same final exit or blocked-input behavior on both hosts. Separate native stack evidence identifies an out-of-bounds access in an original bundled SDK. |
| AquaWar | An original missing-return branch reaches the same native trap under identical binary and input; another original action policy returns the same invalid action for the recorded fish selection. |
| Miracle | An original randomly selected infinite-loop branch reproduces on both hosts with identical input and output; the formal three-second limit applies. |
| Generals | A bundled private extension raises the same IndexError under identical input. A separate policy imports torch at startup and can approach or exceed the one-second first-turn deadline; dependency and functional output parity are verified. |
| Map, clock and native policies | Recorded-map Rollman success, effective-seed Pacman replay and a 385-round clock-controlled LOTA replay provide paired controls. Formal policy RNG and outcomes are retained. Native failure labels are preserved without claiming an unverified exact source line. |

Cold initialization and policies seeded from wall-clock time can change failure frequency and trajectories. Such observations remain in the evidence. Process exit after a terminal result is distinguished from failure during an active turn. Buffer ownership, active-player reply requirements, seed forwarding, compiler selection, dependency imports and admission control have focused regressions. No strategy is removed or replaced to improve acceptance results.

## Local package and native harnesses

The clean-install check creates independent controller and player environments and extracts the published assets. All twelve public game contracts pass, covering 52 actual matches and 22 runnable starter configurations. The AntWar2 public SDK asset installs with 3,693 verified files; its integer wire format passes on Python 3.10 and 3.14, twenty focused tests pass, and five additional local matches complete. Four fresh-interpreter module entrypoints and authenticated hosted health checks pass.

Official Codex 0.147.0 and Claude Code 2.1.231 / Claude Agent SDK 0.2.162 have real-provider acceptance evidence for workspace I/O, native automatic compaction, same-session resume, preserved notes, remote small and complete-pool large evaluations, budget accounting and a frozen final snapshot. Automatic-compaction tests use reduced windows to trigger the native mechanism; formal defaults retain their documented 200,000-token settings. A manual compaction command is not substituted for this evidence.

The component suite records 737 passes, one skip and 32 deselections; the targeted suite records 70 passes and twelve skips. Live acceptance and targeted regressions are listed separately in [the machine-readable report](release-verification.json). Raw private diagnostics are retained by the maintainers, with evidence-file hashes in that report. Private strategies and credentials are not included.

## Limitations

The hosted complete-pool off-policy replay catalogue is **not provisioned**. Local frozen replay catalogues can be generated from the published subset; these are subset-data experiments. Provider compatibility, available model credit and local hardware capacity remain user configuration requirements. Runtime acceptance does not certify every external provider or guarantee identical model benchmark scores.

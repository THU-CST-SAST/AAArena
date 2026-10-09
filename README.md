<div align="center">

# Can AI Agents Learn Their Way to the Top? Evaluating Heuristic Learning in a Long-Running Game Agent Competition

<a href="https://yks23.github.io"><ins>Kaisen Yang</ins></a><sup>1,\*,†</sup>,
<a href="https://aoraku.github.io">Qingle Liu</a><sup>1,\*,†</sup>,
Kejin Wang<sup>1,\*</sup>, Yicheng Zhao<sup>1,\*</sup>, Jieming Li<sup>1,\*</sup>,
Shenghan Zheng<sup>1,\*</sup>, Ruize Yang<sup>1,\*</sup>, Bojun Yang<sup>1,\*</sup>,<br>
Heng Gong<sup>1</sup>, Xiang Gao<sup>1</sup>, Lanyue Zhang<sup>1</sup>, Kaiyu Zhong<sup>1</sup>,
Zhuo Liu<sup>1</sup>, Shaoxuan Li<sup>1</sup>, Chengxi Li<sup>1</sup>, Yong Yan<sup>1</sup>,<br>
Weixuan Zhang<sup>1</sup>, Tianwei Luo<sup>1</sup>, Situ Wang<sup>1</sup>, Youjie Zheng<sup>1</sup>, Sihan Zhao<sup>1</sup>,
Shengyuan Wang<sup>1,2</sup>, Huan-ang Gao<sup>1</sup>, Jiazheng Xu<sup>1</sup>,
Xiaohui Xie<sup>1</sup>, Wentao Han<sup>1</sup>, Hongning Wang<sup>1,‡</sup>

<sup>1</sup> Department of Computer Science and Technology, Tsinghua University<br>
<sup>2</sup> College of AI, Tsinghua University<br>
<sup>\*</sup> Core Contributor &nbsp; <sup>†</sup> Project Lead &nbsp; <sup>‡</sup> Corresponding Author

</div>

**Paper (arXiv):** [arXiv:2610.12341](https://arxiv.org/abs/2610.12341)

**Project homepage:** [aaarena.net](https://aaarena.net)

## Overview

AAArena (Agents for Agents Arena) is a benchmark for **Adversarial Heuristic Learning**: a coding agent reads game rules, writes an executable game-playing policy, plays against archived human opponents, and improves its code from feedback while its model weights remain fixed. Performance is measured by Elo and rank against a frozen, game-specific human opponent pool.

This repository contains the paper's **12 games**, public SDKs, selected human-player code for local practice, Saiblo and AI9 evaluators, official Codex and Claude Code harness integrations, and experiment runners. Game resources are bundled in `assets/*.tar.gz`; no separate game download or Git LFS checkout is required. The experiment presets are in [`configs/paper.json`](configs/paper.json).

| Game | CLI identifier | Game | CLI identifier |
| --- | --- | --- | --- |
| Rollman | `rollman` | Miracle | `miracle` |
| Pacman | `pacman` | Dorado | `dorado` |
| AntWar | `antwar` | MoneCraft | `monecraft` |
| AquaWar | `aquawar` | LOTA | `lota` |
| Generals | `generals` | SnakeGo | `snakego` |
| LostSpace | `lostspace` | AntWar2 | `antwar2` |

Models, games and experiment suites are independently selectable. Supply your own model identifier, compatible API endpoint and credential. The Codex harness requires a compatible Responses endpoint; the Claude Code harness requires a compatible Messages endpoint. The endpoint must support the requested reasoning effort, context length, streaming and tool interfaces. There is no built-in model allowlist. Additional games require an evaluator/resource adapter and a verified opponent pool.

We release code only for human players with even-numbered ranks outside the top eight.

Player-code publication follows the experiment's **frozen measured Elo ranking**: only ranks **10, 12, 14, …** are included. Ranks 1–8 and all odd ranks are withheld. The 12 game packs contain **909 published human-player packages**. Full ranking metadata is available in `results/elo`; each game's `players/publication.json` identifies the released players and their original reference ranks.

**Formal small and large evaluations use the authenticated HTTPS service at `https://101.42.12.204`.** The complete frozen opponent pool stays on the evaluation server. Coding-agent inference, policy editing and replay inspection run locally; the controller uploads a frozen candidate policy and receives feedback. Small evaluations return binary outcomes or dense public trajectories according to the experiment arm. Large evaluations return full-pool Elo, rank and aggregate statistics without dense trajectories.

**Local practice works without an evaluation-service token** against the published subset. Local ranks refer to positions within that subset; `reference_rank` identifies the frozen paper rank. Subset scores must not be reported as complete-pool paper results. See [evaluation access and scope](docs/remote-evaluation.md).

## Installation

The reference host runs **Debian 13 (x86-64 Linux)** with cgroup v2, a working systemd user manager, unprivileged user namespaces, Bubblewrap, GCC/G++ 14.2, Make, CMake and Ruby. Python 3.11 or newer runs the controller; a separate Python 3.10.14 environment runs game participants. Conda is used below to create that environment. Experiments run without root on a configured host. See [`docs/runtime.md`](docs/runtime.md) for sandbox requirements.

With the OS prerequisites and Conda installed, the setup command installs the controller and player dependencies, verifies their identities and asset hashes, checks the actual sandbox and compiler, and runs all 12 local game contracts. It makes no model calls and needs no evaluation API token.

```bash
git clone https://github.com/THU-CST-SAST/AAArena.git
cd AAArena
python3 scripts/setup_local.py
source validation/local-setup/activate.sh
```

Run `python3 scripts/setup_local.py --verify-only` to verify existing environments without installing packages. The activation file is produced only after all checks pass. Extract or clone into an independent directory; hard-linked research copies are not supported by the build sandbox. Native macOS, Windows and ARM execution is not supported; use an x86-64 Linux host or VM for local matches.

For manual installation:

```bash
git clone https://github.com/THU-CST-SAST/AAArena.git
cd AAArena

python3 scripts/install_assets.py
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev,benchmark,claude,games]'

conda create -y --prefix "$PWD/.player-env" --override-channels \
  -c conda-forge python=3.10.14 pip=24.3.1
.player-env/bin/python -m pip install -r environment/player-requirements.txt
cp environment/player-environment-owner.json \
  .player-env/.aa-arena-player-environment.json

python scripts/install_assets.py --verify-only
```

Keep the editable source directory available: the evaluators resolve bundled assets relative to it. Asset installation is offline and verifies archive and per-file SHA-256 digests. Installing language runtimes and dependencies requires network access. AI9 players execute in separate filesystem and network namespaces, including library initialization. A trusted byte relay connects each isolated loader to the judge.

Policy and backend builds use GCC/G++ 14.2 (`gcc-14` / `g++-14`); the configured compilers apply to isolated Make/CMake and plain compiler commands. Compiler identity is part of the build-cache key. Use the same compiler across compared runs. For Python 3.14 Linux, add `-c environment/controller-constraints-py314-linux.txt` to the controller dependency installation to use the validated versions. A virtualenv-based player setup is also available through `scripts/install_player_env.py --python /path/to/python3.10`.

Configure the runtime in each shell:

```bash
unset PYTHONPATH
export AA_ARENA_SYSTEMD_MODE=user
export AA_ARENA_PLAYER_ENV="$PWD/.player-env"
export PYTHONDONTWRITEBYTECODE=1
export OPENBLAS_NUM_THREADS=1
export OMP_NUM_THREADS=1
export AA_ARENA_CPU_POLICY=unlimited
export AA_ARENA_CC=gcc-14
export AA_ARENA_CXX=g++-14
systemctl --user is-system-running
```

The validated runtime uses `unlimited` per-scope CPU quotas; game decision CPU limits remain enforced. The alternative `one_cpu` policy requires delegated CPU control. Use the same CPU policy across a comparison. Shared seat admission controls concurrent evaluations independently of these quotas.

Install the official CLI for the chosen harness:

```bash
# Codex: official native CLI 0.147.0.
python scripts/install_codex_cli.py
export CODEX_BINARY="$PWD/.tools/codex/0.147.0/codex"

# Claude Code: official native CLI 2.1.231, SDK 0.2.162.
python scripts/install_claude_cli.py
export AA_ARENA_CLAUDE_CLI="$PWD/.tools/claude/2.1.231/claude"
```

Both installers verify the official npm distribution's integrity. Only the selected harness's CLI is required.

## API configuration

Create a named profile outside the repository. Replace the endpoint and model placeholders with values accepted by your provider, and set a context window that the endpoint supports.

```bash
export AA_ARENA_PROFILE_DIR="$HOME/.config/aa-arena/models"
export AA_ARENA_BASE_URL="https://YOUR_API_HOST/v1"
read -r -s -p 'API key: ' AA_ARENA_API_KEY
export AA_ARENA_API_KEY
printf '\n'

python scripts/configure_model.py \
  --name default \
  --model YOUR_MODEL_ID \
  --context-window 200000 \
  --reasoning-effort max
```

Use the API base expected by the selected harness: a Responses base commonly ends in `/v1`; the Claude Code base is commonly the host root. The base URL is passed through without rewriting. The profile stores the endpoint and the **name** of the credential environment variable, never the credential value. The repository contains no provider credentials, private relay configuration or preset model identifiers. To compare multiple models, create separate profile names and pass them together to `--profiles`. For distinct credentials or endpoints, use `--api-key-env` and `--base-url-env` when creating each profile.

Profiles and experiment plans default to `max` reasoning effort. Claude Code profiles use `x-api-key` authentication by default; select `--claude-auth-mode bearer` for an endpoint requiring Bearer authentication. Codex transport retries can be configured with `--stream-max-retries`; this setting is independent of model identity.

## Local evaluation

Local practice needs no evaluation-service credential or model API. To inspect the installed public opponent pool or run a standalone practice match:

```bash
python scripts/practice.py --game pacman --list
python scripts/practice.py --game pacman --rank 1 \
  --strategy games/pacman/public_sdk --output runs/practice-pacman
```

## Evaluation API access

Request a personal evaluation token from the maintainers through a GitHub issue; never post tokens in an issue or commit them. Model-provider credentials and evaluation-service credentials are separate. Export the evaluation token only in the controller shell:

```bash
export AA_ARENA_EVAL_URL="https://101.42.12.204"
read -r -s -p 'Evaluation token: ' AA_ARENA_EVAL_TOKEN
export AA_ARENA_EVAL_TOKEN
printf '\n'
```

The endpoint has a trusted IP-address TLS certificate; no domain or disabled certificate verification is required. Each token has a run quota. There is no anonymous evaluation access. The main and ablation commands below route evaluations to the service automatically and fail explicitly if it is unavailable. They never download withheld strategy code. See [API requests, retries and errors](docs/evaluation-api.md).

Check API/harness compatibility before a full experiment. These checks make model API calls:

```bash
# Codex: native tools, file I/O, session recovery and compaction.
aa-arena benchmark doctor --game pacman --model-profile default \
  --run-dir runs/doctor --codex-binary "$CODEX_BINARY"

# Claude Code: tools, small/large matches, recovery and compaction.
# Choose a provider spending ceiling for this check.
export MAX_BUDGET_USD=1
aa-arena-claude --acceptance --game pacman --model-profile default \
  --run-dir runs/claude-acceptance --max-budget-usd "$MAX_BUDGET_USD"
```

Acceptance checks use low effort to test interfaces; formal experiment plans use `max`. The acceptance spending threshold above is not a recommended budget for a full experiment. Claude Code checks its estimated cost between requests; one request, including compaction, can exceed that threshold. Use a provider-side spending cap when a hard limit is required.

For Claude Code experiments, pass `--harness claude` to the experiment runner.


## Native harness acceptance

After configuring both the model API and evaluation token, run the chosen harness against real remote matches:

```bash
python scripts/validate_harness.py --harness codex --game pacman \
  --model-profile default --codex-binary "$CODEX_BINARY" \
  --run-dir runs/native-codex-acceptance
python scripts/validate_harness.py --harness claude --game pacman \
  --model-profile default --max-budget-usd 2 \
  --run-dir runs/native-claude-acceptance
```

These are paid interface tests, not paper experiments. They exercise native **automatic** compaction, session resume, workspace I/O, one real small match and one complete-pool large evaluation, followed by a frozen final snapshot. Each needs a fresh directory. Test thresholds are 20K for Codex and a 100K window for Claude; formal defaults remain 200K. Provider compatibility and sufficient test credit are required. A passing manual `/compact` check alone does not establish automatic-compaction acceptance.

## Running experiments

Run the following examples in Bash from the repository root with `.venv` active. Set `HARNESS` before generating plans. The examples use one model profile; `--profiles profile_a profile_b` creates jobs for multiple profiles.

```bash
export HARNESS=codex                 # codex or claude
export JOBS=4                       # Concurrent coding-agent experiments.
export WORKERS=8                    # Match workers per experiment.
export SEAT_CAPACITY=32              # Shared active-match limit on this host.
export AA_ARENA_ADMISSION_ROOT="$PWD/runs/admission"
mkdir -p plans

run_plan() {
  local extra=()
  if [ "$HARNESS" = claude ]; then
    : "${MAX_BUDGET_USD:?Set an explicit per-experiment API spending ceiling}"
    extra+=(--max-budget-usd "$MAX_BUDGET_USD")
  fi
  python scripts/run_experiments.py run --plan "$1" \
    --jobs "$JOBS" --workers "$WORKERS" --seat-capacity "$SEAT_CAPACITY" \
    --admission-root "$AA_ARENA_ADMISSION_ROOT" \
    --codex-binary "${CODEX_BINARY:-codex}" \
    --claude-binary "${AA_ARENA_CLAUDE_CLI:-claude}" "${extra[@]}" "${@:2}"
}
```

Choose coding-agent concurrency (`JOBS`) to fit local CPU, memory and model API capacity. Hosted match workers and seat capacity are controlled by the service; `WORKERS` and `SEAT_CAPACITY` apply to local evaluation and catalog generation. All launchers and off-policy catalog builders on a host should share one admission root. Give each experiment its own run directory. The launcher rejects incompatible configuration changes when resuming an existing run.

### Main experiments

Each game starts from the public SDK with **128 small-match units and 16 large evaluations over the complete frozen pool**. A small-match request may consume multiple units. The initial baseline is uncharged. The agent may edit its writable strategy and inspect the feedback allowed by its experiment arm.

```bash
python scripts/run_experiments.py plan --suite main \
  --profiles default --harness "$HARNESS" --output plans/main.json
run_plan plans/main.json
```

The default plan covers all 12 games. Select a subset with `--games pacman aquawar`, or request independent repetitions with `--seeds 20260922 20260923 20260924`. Model calls and token use are not the match budget; the Claude spending ceiling is an additional user-selected stopping constraint.

### Continuations

A continuation starts from a **completed 128/16 run** and adds **256/32**, for cumulative totals of **384/48**. It retains the workspace, strategy history, ledger and harness session. Unused first-stage units expire; the extension is applied once. Use the original harness and model profile.

```bash
python scripts/run_experiments.py continuation-plan \
  --run-dir runs/EXISTING_RUN --harness "$HARNESS" \
  --output plans/continuation.json
run_plan plans/continuation.json
```

Continuations are available for every bundled game. The paper's continuation subset is Generals, Miracle, LOTA and AntWar2.

### Opponent-order ablation

Compare model choice, ladder selection starting from frozen pool rank 30, random selection and frozen pool ranks 1–4. Each arm uses small-match batch size 4 and match seed 43.

```bash
python scripts/run_experiments.py plan --suite order \
  --profiles default --harness "$HARNESS" --output plans/order.json
run_plan plans/order.json
```

### Feedback ablation

Compare detailed replay feedback against binary outcome feedback. The binary arm does not expose dense replay files. Both arms use match seed 42.

```bash
python scripts/run_experiments.py plan --suite feedback \
  --profiles default --harness "$HARNESS" --output plans/feedback.json
run_plan plans/feedback.json
```

### Batch-size ablation

Compare small-match batches of 1, 2, 4 and 8 under the same 128/16 budget, using match seed 42.

```bash
python scripts/run_experiments.py plan --suite batch \
  --profiles default --harness "$HARNESS" --output plans/batch.json
run_plan plans/batch.json
```

Order, feedback and batch ablations default to Pacman, AntWar and Miracle. Every suite accepts `--games` to select any subset of the 12 games and `--seeds` for repeated trials.

### Active-match behavior cloning

The agent interacts with a target at frozen full-pool rank 5, 15, 25 or 35, with **32 small-match units and zero large evaluations**. The default plan covers all 12 games.

```bash
python scripts/run_experiments.py plan --suite active-clone \
  --profiles default --harness "$HARNESS" --output plans/active-clone.json
run_plan plans/active-clone.json
```

### Off-policy learning

Generate a frozen human-versus-human replay catalog locally from the published players; see [catalog generation](docs/offline.md). This stage needs no model API. The controller exposes budgeted observations to the learner, without granting it access to human source programs. The hosted off-policy catalog is not provisioned; supply `--catalog-root` with a frozen replay-only catalog. A catalog generated from the public subset is a subset-data experiment, even when large evaluations use the full pool.

```bash
python scripts/run_experiments.py plan --suite offpolicy \
  --profiles default --harness "$HARNESS" \
  --games pacman antwar miracle --output plans/offpolicy.json
run_plan plans/offpolicy.json --catalog-root catalogs
```

The learner receives **128 trajectory views and 16 remote full-pool large evaluations** and cannot initiate its own small matches. `catalogs/GAME/manifest.json` and the referenced replay files must exist for each selected game.

## Results and validation

**Validation:** all three release criteria pass on the documented x86-64 Linux configuration: reference/hosted behavioral parity, authenticated small/full-pool evaluation, and clean local installation across all 12 games. Both official native harnesses pass real automatic-compaction and session-resume checks. Original player failures and timing limits are preserved. See [validation scope and results](validation/README.md) for evidence and limits. The hosted full-pool off-policy replay catalogue is not provisioned.

Formal large evaluations fit the candidate against the complete frozen Elo anchors and report full-pool rank. Local practice uses only published opponents and is a separate result scope. Match randomness, candidate bugs and historical opponent forfeits can affect a score; an infrastructure failure is not a valid model-performance result.

```bash
# Inspect one experiment's Elo, rank, usage and budget state.
aa-arena benchmark report runs/EXISTING_RUN

# Verify game resources and public starter matches without model calls.
python scripts/validate_games.py --jobs 2

# Validate real remote detailed/binary small matches and full-pool large matches.
# Consumes evaluation quota; makes no model API calls.
python scripts/validate_remote.py --output runs/remote-validation --large --jobs 4

# Run component tests.
pytest -q tests/test_public_player_policy.py tests/test_local_subset.py
```

The learner sees public rules, SDKs, designated examples and permitted feedback. Opponent source programs and referee internals stay controller-side. Infrastructure failures are separate from candidate forfeits and must not be reported as model performance. Retain independent repetitions and failure causes when reporting comparisons. See [`docs/initial-state.md`](docs/initial-state.md) for starter-program semantics and [`validation/README.md`](validation/README.md) for validation scope.

Validation details and scope are in [`validation/README.md`](validation/README.md). External model endpoints must pass the compatibility checks before use; stochastic scores need independent repetitions.

## Repository layout

```text
assets/          Game engines, SDKs and filtered player packs and integrity manifests
configs/         Paper experiment presets
docs/            Runtime, resource visibility and off-policy documentation
environment/     Controller and player environment specifications
results/         Frozen human-player availability and reference Elo ladders
scripts/         Installers, experiment launchers and replay catalog builders
src/aa_arena/   Evaluators, match accounting, resource isolation and harnesses
tests/           Component and integration tests
validation/      Verification summaries
```

## Citation

```bibtex
@misc{yang2026aaarena,
  title  = {Can AI Agents Learn Their Way to the Top? Evaluating Heuristic Learning in a Long-Running Game Agent Competition},
  author = {Kaisen Yang and Qingle Liu and Kejin Wang and Yicheng Zhao and Jieming Li and Shenghan Zheng and Ruize Yang and Bojun Yang and Heng Gong and Xiang Gao and Lanyue Zhang and Kaiyu Zhong and Zhuo Liu and Shaoxuan Li and Chengxi Li and Yong Yan and Weixuan Zhang and Tianwei Luo and Situ Wang and Youjie Zheng and Sihan Zhao and Shengyuan Wang and Huan-ang Gao and Jiazheng Xu and Xiaohui Xie and Wentao Han and Hongning Wang},
  year   = {2026},
  eprint = {2610.12341},
  archivePrefix = {arXiv},
  primaryClass = {cs.AI},
  doi    = {10.48550/arXiv.2610.12341},
  url    = {https://arxiv.org/abs/2610.12341}
}
```

## License

The framework is distributed under the [MIT License](LICENSE). Game resources retain their individual provenance, attribution and license files inside each game pack; the framework license does not replace those terms.

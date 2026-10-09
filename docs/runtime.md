# Linux runtime

The controller runs as a normal user. Game participants run in isolated filesystem and network namespaces, with process groups tracked by cgroup v2 and systemd scopes. The host must provide a working user manager and allow unprivileged namespaces. Installing OS packages is separate from running experiments.

The tested reference distribution is Debian 13 on x86-64. OS prerequisites are Bubblewrap, systemd with a working user manager, cgroup v2, GCC/G++ 14.2.0, Make, CMake, Ruby, Python 3.11 or newer, and Conda (or a separately installed Python 3.10.14). The setup command verifies namespace and scope creation before installing Python dependencies:

```sh
python3 scripts/setup_local.py
source validation/local-setup/activate.sh
```

The command performs real local matches for every game without model or evaluation-service calls. It preserves host proxy/package-index settings; those settings must point to reachable services. `--verify-only` uses the installed environments and runs the same runtime, asset and game checks. A failed check returns a nonzero exit status and does not leave an acceptance activation file. Local asset trees must contain independent regular files, without hard links to research or diagnostic copies.

A manual rootless launch uses:

```sh
export AA_ARENA_SYSTEMD_MODE=user
export AA_ARENA_PLAYER_ENV="$PWD/.player-env"
export PYTHONDONTWRITEBYTECODE=1
export OPENBLAS_NUM_THREADS=1
export OMP_NUM_THREADS=1
unset PYTHONPATH
systemctl --user is-system-running
```

The default CPU policy, `one_cpu`, requires a delegated CPU controller and verifies a one-CPU aggregate quota per player scope. When that controller is unavailable, an explicitly declared `AA_ARENA_CPU_POLICY=unlimited` uses no per-scope CPU quota. Game decision CPU limits remain enforced by the evaluators. These policies are different experimental settings; use one policy consistently across compared models, record it, and do not present an unlimited-policy run as quota-controlled. Shared admission limits bound simultaneous matches independently of this policy. A seat lease represents an entire match, including its players and referee, rather than a single player process.

The rootless validation host uses `AA_ARENA_SYSTEMD_MODE=user` and `AA_ARENA_CPU_POLICY=unlimited`. To validate an equivalent host:

```sh
export AA_ARENA_CPU_POLICY=unlimited
AA_ARENA_RUN_ROOTLESS_INTEGRATION=1 pytest -q tests/test_rootless_policy.py tests/test_claude_workspace.py
python scripts/validate_games.py --jobs 2
```

`validate_games.py` executes public starter matches, checks replay translations and resource visibility, and records separate game results. It performs no model calls. An infrastructure failure is a failed validation, not a model loss. Static-only validation is available with `--static-only` and is explicitly labeled as such.

Controller and player dependencies are independent. The controller requires Python 3.11 or newer; the player specification pins Python 3.10.14 and its numerical/game dependencies. Each host should create its own environments from the supplied specifications. Never copy a virtualenv from another machine or rely on a research checkout through `PYTHONPATH`.

The BLAS/OpenMP settings prevent numerical libraries from creating a separate large thread pool in every concurrent controller. The experiment launcher sets both variables to one. Match concurrency is controlled separately by `--jobs`, `--workers` and shared seat admission.

The CLI compatibility doctor and Claude acceptance mode are separate, paid model checks. A resource certificate alone does not verify provider streaming, native tool dispatch, session recovery or conversation compaction. Configure your own model profile and credentials outside the package before those checks.

## Frozen reference system runtime

For cross-host comparisons, operators can mount the same immutable system snapshot in compiler and player sandboxes. The reference profile uses Debian GCC/G++ 14.2.0-19 and glibc 2.41. A compiler version number alone does not identify its headers, standard library or distribution patches.

```sh
python scripts/export_reference_runtime.py --output /srv/arena/runtime/system-v1
export AA_ARENA_REFERENCE_RUNTIME=/srv/arena/runtime/system-v1
export AA_ARENA_CC=gcc-14
export AA_ARENA_CXX=g++-14
python scripts/check_evaluator_host.py
```

Mirror that snapshot to each evaluator host, together with a validated relocatable player-Python environment. `aa-arena-runtime.json` fingerprints the file manifest; startup verifies every file hash and symlink. Host credentials are excluded from the exported `/etc` allowlist. Compilers and players see the snapshot's system libraries, and AI9 judges use its ELF loader and libraries. Build-cache identities include the runtime fingerprint. Without `AA_ARENA_REFERENCE_RUNTIME`, local practice uses the host's toolchain and is not a frozen-runtime parity claim.

AI9 backends receive the registered per-match seed through `AA_ARENA_GAME_SEED`. A policy that uses its own clock, randomness or undefined behavior can still vary; a successful build or a terminal winner alone does not prove absence of a policy error. Cross-host validation must compare actual game artifacts and error records.

For a hosted frozen environment, configure `AA_ARENA_BACKEND_PYTHON` to the absolute `bin/python` path of the trusted game-backend Python bundle. This is separate from `AA_ARENA_PLAYER_ENV`: the API service interpreter does not define game logic dependencies. Python Saiblo backends use this interpreter and, when configured, the reference system's ELF loader and libraries. Native judges retain their configured commands. The evaluator startup check verifies the backend bundle's `runtime-files.json` file hashes and imports NumPy, Matplotlib and ANTLR. An invalid configured interpreter fails startup; no interpreter fallback is used. Without this setting, local practice uses the game adapter's Python command.

## Match concurrency

`--seat-capacity` bounds complete matches. The local experiment launcher counts physical cores within the process CPU affinity, reserves at least a quarter for host work, and budgets two remaining physical cores per simultaneous match. This is a concurrency estimate, not exclusive CPU pinning. For example, 16 logical CPUs on 8 physical cores default to 3 simultaneous matches. `--jobs` and `--workers` may queue more work without raising this shared limit. An explicit capacity override requires representative timeout-sensitive game checks under load; successful HTTP responses alone do not establish safe capacity. Game decision limits remain fixed. Hosted operators use the same meaning for the admission pool capacity and record scheduling configuration with acceptance evidence.

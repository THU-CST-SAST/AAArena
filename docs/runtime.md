# Linux runtime

The controller runs as a normal user. Game participants run in isolated filesystem and network namespaces, with process groups tracked by cgroup v2 and systemd scopes. The host must provide a working user manager and allow unprivileged namespaces. Installing OS packages is separate from running experiments.

A rootless launch uses:

```sh
export AA_ARENA_SYSTEMD_MODE=user
export AA_ARENA_PLAYER_ENV="$PWD/.player-env"
export PYTHONDONTWRITEBYTECODE=1
export OPENBLAS_NUM_THREADS=1
export OMP_NUM_THREADS=1
unset PYTHONPATH
systemctl --user is-system-running
```

The default CPU policy, `one_cpu`, requires a delegated CPU controller and verifies a one-CPU aggregate quota per player scope. When that controller is unavailable, an explicitly declared `AA_ARENA_CPU_POLICY=unlimited` uses no per-scope CPU quota. Game decision CPU limits remain enforced by the evaluators. These policies are different experimental settings; use one policy consistently across compared models, record it, and do not present an unlimited-policy run as quota-controlled. Shared admission limits bound simultaneous match seats independently of this policy.

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

The verified x86-64 Linux toolchain includes glibc 2.35, GCC/G++ 11.4.0, Ruby 3.0.2, Bubblewrap 0.11.0 and systemd 249. Both official CLIs and all player matches run without root privileges on this configured host.

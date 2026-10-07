# Private evaluation API

The server contains the 12 game engines, AI9 and Saiblo evaluators, SDKs, scripts and complete frozen human opponent pools. Candidate models and code iteration run on the caller's machine. Authentication uses a per-user Bearer token; HTTPS is required for remote clients. The API never returns human source programs or private operator logs.

| Endpoint | Purpose |
| --- | --- |
| `GET /v1/health` | Authenticated health and supported games |
| `POST /v1/runs` | Create or reopen a game run, freeze pool identity, declare budgets and feedback |
| `POST /v1/runs/{run}/jobs` | Upload policy files and enqueue a small or large evaluation |
| `GET /v1/runs/{run}/jobs/{submission}` | Poll status, result, public artifacts and receipt |

A run starts with at most 128 small units and 16 large evaluations. Small evaluations consume one unit per opponent; a large evaluation consumes one unit for the complete frozen pool. Submission IDs are idempotency keys: identical retries do not incur a second charge; different content under the same ID is rejected. The completed 128/16 run supports the paper's single additional 256/32 extension.

Small evaluations return either binary win/non-win feedback or detailed public trajectories, according to the run's immutable `feedback` setting. Binary feedback contains no dense trajectory. Large evaluations return Elo, rank, aggregate outcomes and per-opponent statistics, without dense trajectory, following the paper protocol. A candidate forfeit and an infrastructure failure have distinct semantics; a failed job is never a valid Elo result.

Uploaded strategies are directories encoded as a list of `{path,data,executable}` objects, with base64 file contents. Absolute paths, traversal, symlinks and oversized uploads are rejected. Both standalone source files and multi-file buildable policies use this format. The evaluated strategy must implement the selected game's SDK interface. A completed response contains `result`, optional public `artifacts` and an integrity `receipt`.

A failed response includes `error.code`, `error.phase`, `error.message`, `error.submission_id` and `error.retryable`. Full compiler/backend diagnostics are private operator logs; public errors do not expose opponent source, credentials or server paths. Retry the exact same submission only when `retryable` is true. A corrected policy uses a new submission ID in the same run and consumes a new budget unit; completed failures do not block policy correction. Only one queued or running job is allowed per run. Recoverable infrastructure failures reserve their original budget, and successful recovery does not charge again.

Configure the service URL and token through `AA_ARENA_EVAL_URL` and `AA_ARENA_EVAL_TOKEN`, then submit a policy directory:

```bash
python scripts/evaluate_policy.py --game pacman --kind small \
  --strategy /path/to/policy --feedback detailed --opponents OPPONENT_ID \
  --run-id example-run --submission-id small-001 --output results/small-001.json
python scripts/evaluate_policy.py --game pacman --kind large \
  --strategy /path/to/policy --feedback detailed \
  --run-id example-run --submission-id large-001 --output results/large-001.json
```

`OPPONENT_ID` comes from the inventory returned by `POST /v1/runs`. Polling timeouts leave accepted jobs on the server; use the same run and submission IDs to recover. Independent experiments use separate run IDs. No Tencent Cloud credentials are required by API users.

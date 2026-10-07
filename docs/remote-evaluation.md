# Evaluation access and scope

Formal benchmark runs use `https://101.42.12.204` with a personal Bearer token in `AA_ARENA_EVAL_TOKEN`. `AA_ARENA_EVAL_URL` can select another compatible HTTPS deployment. Ask the repository maintainers for access; tokens have per-user run quotas and are never included in the repository. Model API credentials are configured independently.

The controller uploads only the frozen candidate policy. The service retains all human strategies and executes AI9 or Saiblo matches. The client verifies the complete ranking metadata, pool identity, request/result hashes and replay checksums. A network failure leaves the accepted submission recoverable under its original ID; there is no automatic fallback to local subset scores.

Small-match feedback is fixed per experiment: binary win/non-win outcomes, or detailed public trajectories. Full-pool large evaluations return Elo, rank and per-opponent statistics without dense trajectories. The 128/16 main budget and one additional 256/32 continuation are enforced on the service. See [API specification](evaluation-api.md).

`practice.py` and the local evaluator operate on the published subset without a service token. The local leaderboard numbers published opponents consecutively; `reference_rank` preserves their complete-pool rank. Resource bundles for formal agents use complete ranking metadata while containing only the permitted public example code. Existing local-subset runs cannot resume as formal runs.

我们只公布未进入决赛圈的偶数人类选手的代码。

Only frozen measured Elo ranks greater than eight and even are distributed. Full ranking metadata does not grant access to withheld programs. Off-policy catalog download is supported by the protocol, but the hosted catalog is not provisioned. Use an explicitly supplied frozen replay-only catalog and report its population.

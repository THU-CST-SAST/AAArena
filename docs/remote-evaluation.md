# Evaluation scope

Local small and large evaluations execute against the published subset. Each game includes human strategies whose frozen experiment Elo rank is greater than eight and even. No private opponent download is required or provided.

The local leaderboard numbers published opponents consecutively from one. `reference_rank` records each opponent’s rank in the complete frozen reference table. Local large evaluations fit the candidate against the available Elo anchors and report subset rank and Elo, not complete-pool paper results. Rank-targeted ablations operate on local positions. Ladder starts at min(30, pool size); cloning includes only available target positions from 5, 15, 25 and 35.

Complete-pool small and large evaluation requires a hosted service. **The service and its public API are not implemented or available in this release.** The intended service accepts candidate strategies and returns match feedback while keeping withheld opponent programs server-side. No service URL, token or complete-pool reproduction command is offered here.

我们只公布未进入决赛圈的偶数人类选手的代码。

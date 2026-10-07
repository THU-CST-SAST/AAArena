# Reference evaluation data

These files are runtime inputs for the paper's 12 games:

- `availability/`: frozen player availability records.
- `elo/`: frozen game-specific reference ratings and rankings.

The complete contestant source archives are in `assets/`. The resource builder combines these reference records with the game packs to construct the public leaderboard and controller-side evaluation pool. Every experiment seals the selected pool in its ledger directory.

Run reports are produced by `aa-arena benchmark report runs/EXISTING_RUN`.

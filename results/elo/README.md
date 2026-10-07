# Frozen reference ratings

Each of the 12 game directories contains `measured_elo.json` and `measured_ranking.tsv`. These game-specific reference ratings define the fixed opponent scale used by the paper protocol. The resource builder selects verified opponents and publishes their identifiers, ranks and ratings to the learner.

A challenger is evaluated against the full frozen verified pool. Its rating uses a Bradley–Terry fit on the 400-point Elo scale, with one neutral-anchor prior for the entire pool. Its rank is one plus the number of frozen opponents with strictly greater Elo. Evaluator and infrastructure failures must be resolved before a run supplies a reported score.

Ratings from different games have different reference scales. Compare models within a game. Every run records its exact pool and snapshot identities. Use `aa-arena benchmark report runs/EXISTING_RUN` to inspect a completed experiment.

# Initial strategy semantics

Main-table experiments initialize the writable strategy from the public SDK. The coding agent may edit the copied client and strategy files. The controller's public resource bundle, hidden opponent programs and referee are immutable experiment inputs. A public starter is an initial program, not a claim of competitive strength or correctness against every opponent.

## Miracle

The paper's public Python SDK file `games/miracle/public_sdk/gameunit.py` has SHA-256 `62f13246b2b047a5c96edd5e557a626e6dc3c76a3df937a8b12708998d9d76e7`. Its `UNIT_TYPE` table contains six entries and its `ARTIFACT_NAME` table contains three entries. The bundled referee protocol also permits `FrostDragon` (unit type 6) and `WindBlessing` (artifact type 3). An unedited starter can therefore raise `IndexError` while decoding a legal opponent state. The referee records that exit as a candidate forfeit.

Each run starts from this public SDK. The agent may repair its writable strategy during the declared budget. A completed strategy is not the starting program for a fresh main-table run.

Full-pool validation exercises this initial program and records candidate forfeits separately from infrastructure failures. A forfeit from the program does not mean a model experiment has been evaluated; no model calls are made by that validation. Published model scores come from the eligible evaluated strategies produced during the declared iteration budget.

## Failure accounting

Candidate build, parsing, action and decision-time failures are attributed to the candidate according to the game protocol. Opponent failures are attributed separately. A referee crash, missing dependency, sandbox failure or unresolved match result must fail the evaluation rather than supply a model score. Validation receipts report the initial strategy's candidate-error count explicitly.

# Validation scope

The asset audit verifies all 12 archives, file checksums and the exact 909 permitted player packages against the frozen measured Elo tables. The installer refuses extra opponent packages in an existing installation.

The local-subset tests check every game’s resource bundle, frozen opponent inventory, reference-rank mapping and generated main/ablation target availability. Runtime checks use x86-64 Linux with user systemd scopes, the bundled game engines and the published player subset. Detailed results are listed in `release-verification.json`.

Model endpoint compatibility depends on the user’s provider. Native CLI fixture tests validate tool I/O, budgeted evaluations and session handling without paid model calls. These checks do not assert equality between local subset scores and complete-pool paper scores. The complete-pool evaluation service is not available in this release.

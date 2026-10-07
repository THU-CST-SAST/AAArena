"""Explicit direct subprocess launcher for backends, tests, and local diagnostics."""

from __future__ import annotations

import subprocess

from .model import LaunchMetadata, ManagedProcess, ProcessSpec, safe_environment


class DirectLauncher:
    """Launch without resource isolation; formal player paths must select another launcher."""

    def preflight(self) -> None:
        return None

    def start(
        self,
        spec: ProcessSpec,
        *,
        match_id: str,
        player_index: int,
    ) -> ManagedProcess:
        process = subprocess.Popen(
            spec.argv,
            cwd=spec.cwd,
            env=safe_environment(spec.env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        return ManagedProcess(
            process,
            LaunchMetadata(
                launcher="direct",
                match_id=str(match_id),
                player_index=int(player_index),
            ),
        )

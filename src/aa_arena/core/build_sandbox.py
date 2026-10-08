"""Fail-closed isolation for candidate build recipes and compiler inputs.

The writable root must be a disposable staging copy, never the submitted tree.
Additional read-only paths are a trusted caller's explicit SDK declaration, not
paths inferred from compiler arguments or candidate diagnostics.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Sequence
from .reference_runtime import system_path


BUILD_SANDBOX_POLICY_VERSION = 3
_SYSTEM_PATH = "/usr/bin:/bin"
_ENV = {"PATH": _SYSTEM_PATH, "HOME": "/tmp/home", "TMPDIR": "/tmp", "LANG": "C"}


class BuildSandboxError(RuntimeError):
    """Isolation is unavailable or the candidate tree is unsafe to process."""


def validate_build_tree(root: Path) -> None:
    """Reject links/devices before any host-side copy, recovery, or publication.

    Do not follow links, even to test whether their targets exist. A recipe may
    create one between compiler attempts, including at a metadata destination.
    """
    pending = [Path(root)]
    while pending:
        path = pending.pop()
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            pending.extend(path.iterdir())
        elif not stat.S_ISREG(mode) or path.lstat().st_nlink != 1:
            raise BuildSandboxError(f"unsafe build tree entry (link or special file): {path}")


def _make_staging_owner_writable(root: Path) -> None:
    """Repair copytree-preserved frozen modes only on disposable staging.

    Bwrap maps the launching owner to UID 65534; it cannot override 0444/0555
    modes. Include existing outputs as well as directories, preserve execute
    bits, and never grant additional group/other permissions or follow links.
    """
    pending = [root]
    while pending:
        path = pending.pop()
        info = path.lstat()
        permissions = stat.S_IMODE(info.st_mode) | stat.S_IRUSR | stat.S_IWUSR
        if stat.S_ISDIR(info.st_mode):
            path.chmod(permissions | stat.S_IXUSR, follow_symlinks=False)
            pending.extend(path.iterdir())
        elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
            path.chmod(permissions, follow_symlinks=False)
        else:
            raise BuildSandboxError(f"unsafe build tree entry (link or special file): {path}")


def run_isolated_build(
    command: Sequence[str],
    *,
    cwd: Path,
    build_root: Path | None = None,
    readonly_paths: Sequence[Path] = (),
    timeout: float = 300.0,
) -> subprocess.CompletedProcess[str]:
    """Run a build with only staging, declared SDK files, and OS toolchains.

    Paths retain their absolute spelling for Make/CMake and SDK include diagnostics.
    No host parent, home, controller, environment, network, or temporary directory
    is exposed. Missing/broken bubblewrap never falls back to a host subprocess.
    """
    bwrap = shutil.which("bwrap", path=_SYSTEM_PATH)
    if bwrap is None:
        raise BuildSandboxError("candidate build isolation requires bubblewrap (bwrap)")
    root = Path(build_root if build_root is not None else cwd).resolve()
    work_cwd = Path(cwd).resolve()
    if not work_cwd.is_relative_to(root):
        raise BuildSandboxError("build cwd is outside staging")
    validate_build_tree(root)
    arguments = [
        bwrap,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--unshare-user",
        "--uid",
        "65534",
        "--gid",
        "65534",
        "--cap-drop",
        "ALL",
        "--disable-userns",
    ]
    # Deliberately exclude /usr/local (host applications), /etc (credentials),
    # and /usr/share generally. These are packaged system toolchain components.
    for name in ("/usr/bin", "/usr/lib", "/usr/lib64", "/usr/libexec", "/usr/include"):
        source = system_path(name)
        if source.exists():
            arguments += ["--ro-bind", str(source), name]
    for pattern in ("cmake*", "gcc*", "pkgconfig", "aclocal*"):
        for path in sorted(system_path("/usr/share").glob(pattern)):
            arguments += ["--ro-bind", str(path), '/usr/share/' + path.name]
    for name in ("bin", "lib", "lib64"):
        path = system_path('/' + name)
        if path.is_symlink():
            arguments += ["--symlink", os.readlink(path), '/' + name]
        elif path.exists():
            arguments += ["--ro-bind", str(path), '/' + name]
    # Debian compiler and awk aliases go through /etc/alternatives. Expose only
    # links whose final targets are system executables, not the alternatives tree.
    for name in ("cc", "c++", "gcc", "g++", "cpp", "awk", "nawk"):
        alias = system_path('/etc/alternatives/' + name)
        if alias.is_symlink():
            target = os.readlink(alias)
            if target.startswith('/usr/bin/'):
                arguments += ["--symlink", target, '/etc/alternatives/' + name]
    arguments += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/tmp/home"]
    sdk_paths: list[Path] = []
    for raw_path in readonly_paths:
        path = Path(raw_path).resolve(strict=True)
        if path == root or root.is_relative_to(path) or path.is_relative_to(root):
            raise BuildSandboxError("SDK mount overlaps writable staging")
        if path.is_dir():
            validate_build_tree(path)
        sdk_paths.append(path)
    try:
        _make_staging_owner_writable(root)
        # Root loses DAC override inside bwrap's user namespace. Mirror only
        # the declared mount roots in a private mount namespace, so private
        # ancestors owned by another user need not become traversable. Reuse
        # the arena's trusted mount setup helper; it execs bwrap before any
        # candidate command is run. Non-root callers need no mount mirrors.
        with tempfile.TemporaryDirectory(prefix="ahl-build-mount-", dir="/tmp") as temporary:
            gate = Path(temporary)
            build_environment = dict(_ENV)
            toolchain = gate / "toolchain"
            for variable, aliases, make_variable in (
                ("AA_ARENA_CC", ("gcc", "cc"), "CC"),
                ("AA_ARENA_CXX", ("g++", "c++"), "CXX"),
            ):
                configured = os.environ.get(variable)
                if not configured:
                    continue
                executable = shutil.which(configured, path=_SYSTEM_PATH)
                if executable is None or not Path(executable).resolve().is_relative_to('/usr/bin'):
                    raise BuildSandboxError(f"unavailable system compiler: {variable}")
                toolchain.mkdir(exist_ok=True)
                for alias in aliases:
                    wrapper = toolchain / alias
                    wrapper.write_text('#!/bin/sh\nexec ' + shlex.quote(executable) + ' "$@"\n')
                    wrapper.chmod(0o555)
                build_environment[make_variable] = '/toolchain/bin/' + aliases[0]
            if toolchain.exists():
                arguments += ["--ro-bind", str(toolchain), "/toolchain/bin"]
                build_environment['PATH'] = '/toolchain/bin:' + _SYSTEM_PATH
            plan = []
            for index, (path, flag) in enumerate(
                [(p, "--ro-bind") for p in sdk_paths] + [(root, "--bind")]
            ):
                mount_source = path
                if os.geteuid() == 0:
                    mount_source = gate / f"mount-{index}"
                    if path.is_dir():
                        mount_source.mkdir()
                    else:
                        mount_source.touch()
                    plan.append({"source": str(path), "target": str(mount_source)})
                arguments += [flag, str(mount_source), str(path)]
            arguments += ["--chdir", str(work_cwd), "--remount-ro", "/", "--clearenv"]
            for key, value in build_environment.items():
                arguments += ["--setenv", key, value]
            arguments += ["--", *command]
            if plan:
                plan_path = gate / "mounts.json"
                plan_path.write_text(json.dumps(plan), encoding="utf-8")
                helper = Path(__file__).resolve().parents[1] / "sandbox/private_mount.py"
                arguments = [
                    "/usr/bin/unshare",
                    "--mount",
                    "--propagation",
                    "private",
                    "/usr/bin/python3",
                    "-I",
                    str(helper),
                    str(plan_path),
                    "--",
                    *arguments,
                ]
            completed = subprocess.run(
                arguments,
                cwd="/",
                env=_ENV,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=timeout,
                close_fds=True,
                start_new_session=True,
            )
    except OSError as exc:
        raise BuildSandboxError(f"cannot start candidate build sandbox: {exc}") from exc
    validate_build_tree(root)
    # Return the logical command so existing build diagnostics remain useful.
    return subprocess.CompletedProcess(
        tuple(command), completed.returncode, completed.stdout, completed.stderr
    )

"""Small helpers for launching child processes without a console window.

Codex is commonly installed as a ``.cmd`` shim on Windows.  Starting that
shim with the default ``subprocess`` settings briefly creates a console window
on every invocation, which is particularly noticeable for ``doctor`` and the
app-server client.  Keep the platform-specific launch options in one place so
all Codex entry points behave consistently while non-Windows launches retain
their existing arguments.
"""

from __future__ import annotations

import os
from pathlib import Path
import platform
import subprocess
from typing import Any


# Capture the host's concrete path class once.  Tests commonly emulate
# Windows by monkeypatching ``os.name``; calling ``Path(...)`` after that
# patch would try to construct an unsupported WindowsPath on a POSIX host.
_NATIVE_PATH = type(Path())


def hidden_console_subprocess_kwargs(
    *,
    new_process_group: bool = False,
) -> dict[str, Any]:
    """Return Windows launch options that keep a child console hidden.

    ``CREATE_NO_WINDOW`` is the primary protection for console applications.
    ``STARTUPINFO``/``SW_HIDE`` covers wrappers such as ``codex.cmd`` and is
    harmless for a process that does not create a console of its own.  The
    options are omitted entirely on non-Windows platforms so callers remain
    compatible with existing fake ``run``/``Popen`` functions and preserve
    their prior behavior.

    ``new_process_group`` is needed by the long-lived app-server child so it
    can be terminated independently; short-lived CLI invocations do not need
    it.
    """

    if os.name != "nt":
        return {}

    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if new_process_group:
        flags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    kwargs: dict[str, Any] = {"creationflags": flags}

    # These attributes are available on Windows.  The guarded lookup keeps
    # tests that emulate ``os.name == 'nt'`` on another host and lightweight
    # fake subprocess modules from failing just while constructing options.
    startupinfo_factory = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_factory is not None:
        startupinfo = startupinfo_factory()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["startupinfo"] = startupinfo

    return kwargs


def npm_codex_native_executable(
    shim: str | os.PathLike[str] | None,
) -> str | None:
    """Resolve the native binary bundled behind an npm ``codex.cmd`` shim.

    Calling the shim is safe with the hidden-window kwargs above, but it still
    creates an unnecessary ``cmd.exe -> node.exe`` chain.  The official npm
    package ships the same native executable as an optional platform package;
    use it directly when its well-known layout is present.

    WindowsApps also exposes a ``codex.exe`` path through ``PATH`` on some
    systems, but direct execution of that package-private resource can fail
    with ``Access is denied``.  This resolver only searches beside the npm
    shim and therefore never selects that alias.
    """

    if os.name != "nt" or not shim:
        return None
    shim_path = (
        shim
        if isinstance(shim, _NATIVE_PATH)
        else _NATIVE_PATH(os.fspath(shim))
    )
    machine = platform.machine().casefold()
    if machine in {"arm64", "aarch64"}:
        package_name = "codex-win32-arm64"
        target = "aarch64-pc-windows-msvc"
    else:
        package_name = "codex-win32-x64"
        target = "x86_64-pc-windows-msvc"

    npm_root = shim_path.parent
    package_root = npm_root / "node_modules" / "@openai" / "codex"
    candidates = (
        package_root
        / "node_modules"
        / "@openai"
        / package_name
        / "vendor"
        / target
        / "bin"
        / "codex.exe",
        npm_root
        / "node_modules"
        / "@openai"
        / package_name
        / "vendor"
        / target
        / "bin"
        / "codex.exe",
        package_root / "vendor" / target / "bin" / "codex.exe",
    )
    for candidate in candidates:
        try:
            if candidate.is_file():
                return str(candidate)
        except OSError:
            continue
    return None

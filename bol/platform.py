"""bol.platform — the one place that knows which OS we are running on.

BedrockOnLinux started life Linux-only: the game runs through GDK-Proton inside
umu (the Steam Linux Runtime). That engine cannot run on macOS, so the macOS
port keeps everything that is portable — the launcher, the binary patches, the
DLL shims (XCurl / libHttpClient / cryptbase / GameInput) and the native
Microsoft sign-in — and swaps only the engine for a macOS-native Wine (Game
Porting Toolkit / CrossOver / plain Wine), driven from ``bol.winemac``.

This module isolates the per-OS bits the rest of the package needs — data
directory, package-manager hint, screen size, process control, opening a file
or URL — so every other module can stay platform-agnostic.

It imports only the standard library so the lowest layer (``bol.config``) can
import it without creating a cycle.
"""
# SPDX-License-Identifier: MIT

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SYSTEM = sys.platform
IS_MAC = SYSTEM == "darwin"
IS_LINUX = SYSTEM.startswith("linux")


def data_home(app: str) -> Path:
    """Per-OS data directory for ``app``, honouring the ``BOL_HOME`` override.

    Linux: ``$XDG_DATA_HOME`` or ``~/.local/share``.
    macOS: ``~/Library/Application Support`` (the platform convention).
    """
    env = os.environ.get("BOL_HOME")
    if env:
        return Path(env).expanduser()
    if IS_MAC:
        return Path.home() / "Library" / "Application Support" / app
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / app


_PM_LINUX = (
    ("apt-get", "sudo apt install {}"),
    ("dnf", "sudo dnf install {}"),
    ("pacman", "sudo pacman -S {}"),
    ("zypper", "sudo zypper in {}"),
)


def pm_hint() -> str:
    """An ``"install: {}"``-style template for the host package manager, so
    messages can suggest the right command (``brew`` on macOS)."""
    if IS_MAC:
        return "brew install {}"
    for pm, hint in _PM_LINUX:
        if shutil.which(pm):
            return hint
    return "install: {}"


def screen_wh():
    """Primary screen ``(W, H)`` as strings, or ``None``.

    Used only for gamescope sizing, which is Linux-specific — on macOS the Wine
    Cocoa driver presents the window itself and never needs gamescope, so this
    returns ``None`` there.
    """
    if not IS_LINUX or not shutil.which("xrandr"):
        return None
    try:
        out = subprocess.run(["xrandr"], capture_output=True, text=True,
                             timeout=5).stdout
    except Exception:
        return None
    m = re.search(r"current\s+(\d+)\s+x\s+(\d+)", out)
    return (m.group(1), m.group(2)) if m else None


def kill_pattern(pattern: str):
    """Best-effort SIGKILL of processes whose command line matches ``pattern``.

    Uses ``pkill`` when available (always present on macOS), falling back to a
    ``/proc`` scan on Linux hosts without it. macOS has no ``/proc``, so the
    fallback is Linux-only.
    """
    if shutil.which("pkill"):
        subprocess.run(["pkill", "-9", "-f", pattern],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return
    if not IS_LINUX:
        return
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            cl = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ")
            if pattern.encode() in cl:
                os.kill(int(pid), 9)
        except Exception:
            pass


def open_path(target: str):
    """Open a file, folder or URL in the desktop's default handler.
    ``open`` on macOS, ``xdg-open`` on Linux. Best-effort, never raises."""
    opener = "open" if IS_MAC else "xdg-open"
    try:
        subprocess.Popen([opener, str(target)], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
    except Exception:
        pass

"""bol.winemac — the macOS Windows-runtime backend.

On Linux the game runs through GDK-Proton + umu (the Steam Linux Runtime).
Neither exists on macOS, so there we drive a macOS-native Wine instead. We
detect, in order, Apple's **Game Porting Toolkit** (best D3D→Metal translation,
Apple Silicon via Rosetta 2), then **CrossOver**, then a plain Homebrew / WineHQ
**Wine** — or use an explicit path the user configured (``settings['wine']`` or
``$BOL_WINE``).

What this backend does *not* change: the binary patches, the DLL shims (XCurl,
libHttpClient, cryptbase, GameInput) and the host-side Microsoft pre-auth all
ride on top of whichever Wine we pick, exactly as on Linux — they operate on
files in the prefix / game folder and on Windows PE images, which are the same
under any Wine.

What it *cannot* promise: the WineGDK XUser fork that makes the in-game
Microsoft sign-in work on Linux is compiled into GDK-Proton and has **no macOS
build**. Until a macOS WineGDK engine exists, expect offline / LAN play to work
and the in-game Microsoft login to be unverified. See the README (macOS) for the
current state.

The public contract mirrors ``bol.prefix.proton_umu_cmd`` so the shared
auth / launch / gameinput code can drive either backend the same way.
"""
# SPDX-License-Identifier: MIT

import os
import shutil
import subprocess
from pathlib import Path

from .log import BolError, die, info, ok, warn
from .platform import IS_MAC
from .util import load_settings, save_settings

# CrossOver ships its bottled Wine here on a stock install.
_CROSSOVER_WINE = ("/Applications/CrossOver.app/Contents/SharedSupport/"
                   "CrossOver/bin/wine")


def _gptk_wine():
    """Path to Apple Game Porting Toolkit's wine, if installed via Homebrew.

    ``brew install apple/apple/game-porting-toolkit`` puts a ``wine64``/``wine``
    under the formula prefix and a ``gameportingtoolkit`` wrapper on PATH; we
    locate the real wine next to either."""
    wrapper = shutil.which("gameportingtoolkit")
    if wrapper:
        d = Path(wrapper).resolve().parent
        for w in ("wine64", "wine"):
            if (d / w).exists():
                return d / w
    if shutil.which("brew"):
        try:
            r = subprocess.run(["brew", "--prefix", "game-porting-toolkit"],
                               capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                base = Path(r.stdout.strip())
                for w in ("bin/wine64", "bin/wine"):
                    if (base / w).exists():
                        return base / w
        except Exception:
            pass
    return None


def detect_wine():
    """Return ``(backend, wine_path)`` for the best available macOS Wine, or
    ``(None, None)``. An explicit override (``$BOL_WINE`` or ``settings['wine']``)
    wins; otherwise GPTK ▸ CrossOver ▸ plain Wine."""
    s = load_settings()
    override = os.environ.get("BOL_WINE") or s.get("wine_override") or s.get("wine")
    if override:
        p = Path(override).expanduser()
        if p.exists():
            return (s.get("wine_backend") or "custom", p)
        warn(f"Configured wine '{p}' not found — auto-detecting instead.")
    gp = _gptk_wine()
    if gp:
        return ("gptk", gp)
    if Path(_CROSSOVER_WINE).exists():
        return ("crossover", Path(_CROSSOVER_WINE))
    for cand in ("wine64", "wine"):
        w = shutil.which(cand)
        if w:
            return ("wine", Path(w))
    return (None, None)


def wine_bin():
    """The wine binary recorded in settings by :func:`ensure_wine`, or ``None``."""
    p = load_settings().get("wine")
    return Path(p) if p else None


def wineserver_bin(wine=None):
    """The ``wineserver`` next to ``wine`` (used to tear a prefix down cleanly)."""
    wine = Path(wine or wine_bin() or "")
    cand = wine.parent / "wineserver"
    return cand if cand.exists() else None


def _backend_env(backend):
    """Extra environment a backend wants. GPTK needs its D3DMetal layer and
    Rosetta AVX advertising; every value is a ``setdefault`` so the user (or the
    caller) can override it from the host environment."""
    env = {}
    if backend in ("gptk", "crossover"):
        # esync/msync: lighter Wine synchronisation, big win on macOS.
        env.setdefault("WINEESYNC", "1")
        env.setdefault("WINEMSYNC", "1")
    if backend == "gptk":
        # D3DMetal's shader path needs AVX, which Rosetta only exposes when
        # asked; without it D3D11/D3D12 titles fall over at device creation.
        env.setdefault("ROSETTA_ADVERTISE_AVX", "1")
        env.setdefault("MTL_HUD_ENABLED", "0")
    return env


def mac_wine_cmd(exe, prefix=None):
    """Build ``(argv, env)`` to run a Windows program or verb under the macOS
    Wine. Same shape as :func:`bol.prefix.proton_umu_cmd` — ``exe`` may be an
    absolute ``.exe`` path or a Wine verb (``reg`` / ``wineboot`` / ``msiexec``).
    """
    from .config import PFX
    if not IS_MAC:
        raise BolError("winemac.mac_wine_cmd called off macOS")
    wine = wine_bin()
    if not wine:
        die("No macOS Wine configured — run Install / Update first.")
    prefix = Path(prefix or PFX)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["WINEPREFIX"] = str(prefix)
    env.setdefault("WINEDEBUG", "fixme-all")
    env.update(_backend_env(load_settings().get("wine_backend", "wine")))
    return [str(wine), str(exe)], env


def kill_prefix(prefix):
    """Kill every process in ``WINEPREFIX`` the clean Wine way: ``wineserver -k``.
    macOS has no ``/proc`` to scan, and this is more reliable anyway."""
    ws = wineserver_bin()
    if not ws:
        return
    env = dict(os.environ)
    env["WINEPREFIX"] = str(prefix)
    try:
        subprocess.run([str(ws), "-k"], env=env, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30)
    except Exception:
        pass


def ensure_wine(force=False):
    """Detect a macOS Wine and record it (and its backend) in settings. Dies
    with install hints when none is found. ``force`` re-detects."""
    s = load_settings()
    if not force and s.get("wine") and Path(s["wine"]).exists():
        info(f"macOS Wine backend ready: {s.get('wine_backend', '?')} "
             f"({s['wine']}).")
        s["proton_source"] = "winemac"
        s["native_login"] = True
        save_settings(s)
        return Path(s["wine"])
    backend, wine = detect_wine()
    if not wine:
        die("No Windows runtime found for macOS. Install one of:\n"
            "  • Game Porting Toolkit (recommended, Apple Silicon):\n"
            "      brew install apple/apple/game-porting-toolkit\n"
            "  • CrossOver:  https://www.codeweavers.com/crossover\n"
            "  • Wine:       brew install --cask wine-stable\n"
            "Then re-run Install / Update. To point at a specific build, set\n"
            "  BOL_WINE=/path/to/wine   (or the Wine path in Settings).")
    s = load_settings()
    s["wine"] = str(wine)
    s["wine_backend"] = backend
    # Treat the macOS Wine like a 'custom' engine: launch/patch must NOT apply
    # the Proton-only combase/ntdll byte offsets (different Wine, and it's a
    # shared system install we must not modify in place).
    s["proton_source"] = "winemac"
    s["native_login"] = True
    save_settings(s)
    ok(f"macOS Wine backend: {backend} ({wine})")
    if backend == "wine":
        warn("Using a plain Wine — for a GDK/D3D title, Game Porting Toolkit "
             "(D3DMetal) or CrossOver render far better.")
    return wine

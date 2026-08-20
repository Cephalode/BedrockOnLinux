"""bol.games — Minecraft edition listing, installation and selection."""
# SPDX-License-Identifier: MIT

import json
import re
import shutil
import time
from pathlib import Path

from . import xodus
from .config import CONTENT, GAMES
from .log import BolError, die, info, ok, warn
from .util import load_settings, save_settings


_INSTALL_METADATA = ".bedrock-on-linux-install.json"


def list_editions(include_beta=True):
    """The Minecraft editions available for installation."""
    return [entry for entry in xodus.list_editions()
            if include_beta or not entry["beta"]]


def version_dir(edition_id, version):
    return GAMES / edition_id / version


def list_versions(edition_id):
    """Installable builds for an edition, newest first.

    Each entry gains ``installed``: whether that exact build is already on
    disk, which is what lets switching back to a build you already have cost
    nothing.
    """
    out = []
    for entry in xodus.version_catalogue(edition_id):
        entry = dict(entry)
        entry["installed"] = _game_root(
            version_dir(edition_id, entry["version"])) is not None
        out.append(entry)
    return out


def _game_root(dest):
    """Folder of a complete installed build (exe + appxmanifest), else None
    (a bare exe with no manifest means a truncated install → reinstall).

    Defined in bol.xodus, which is what writes the directory and now has to
    tell a finished download from one that installed nothing."""
    return xodus.game_root(dest)


def _install_record(dest):
    try:
        return json.loads(
            (Path(dest) / _INSTALL_METADATA).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}


def _write_install_record(dest, edition, version, url):
    record = {
        "schema": 2,
        "edition": edition["id"],
        "product": edition["product"],
        "version": version,
        "source_url": url,
        "xodus_rev": xodus.XODUS_REV,
        "installed": int(time.time()),
    }
    target = Path(dest) / _INSTALL_METADATA
    staged = target.with_name("." + target.name + ".tmp")
    try:
        staged.write_text(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8")
        staged.replace(target)
    finally:
        staged.unlink(missing_ok=True)


def _configured_legacy_root():
    """A complete install configured before the move to the Store, or None.

    Anything under GAMES/<edition>/ belongs to the new layout and is handled
    by the ordinary path; this is only about the copy an upgrade inherits.
    """
    configured = (load_settings().get("game_dir") or "").strip()
    if not configured:
        return None
    path = Path(configured)
    try:
        # Legacy installs also live under GAMES, as GAMES/<version-tag>/, so
        # what marks the new layout is the edition id, not the parent.
        owner = path.resolve().relative_to(GAMES.resolve()).parts[0]
    except (ValueError, IndexError, OSError):
        owner = None
    if owner and xodus.edition(owner):
        return None
    return _game_root(path)


def install_game(edition, version=None, progress=None, force=False):
    """Install one build of one edition through Xodus.

    Each build lives in its own folder, so going back to a build already on
    disk costs nothing and the delta cache Xodus keeps beside it stays valid.
    ``xodus-cli streaming`` is itself incremental and atomic -- it compares
    local segment hashes against the package, fetches only what changed and
    commits with a rename -- so there is deliberately no staging dance here.
    """
    catalogue = list_versions(edition["id"])
    if not catalogue:
        raise BolError(
            f"No {edition['name']} build is listed. Check the network "
            "connection and try again.")
    wanted = str(version or "").strip()
    entry = next((c for c in catalogue if c["version"] == wanted), None)
    if entry is None:
        if wanted:
            warn(f"{edition['name']} {wanted} is no longer listed; using "
                 f"{catalogue[0]['version']} instead.")
        entry = catalogue[0]

    dest = version_dir(edition["id"], entry["version"])
    dest.parent.mkdir(parents=True, exist_ok=True)
    root = _game_root(dest)
    if root and not force:
        info(f"{edition['name']} {entry['version']} already installed")
        return root

    # A build installed before the move to the Store lives outside
    # GAMES/<edition>/<version>/ and cannot be reached by this path. It is
    # still a complete, working game and it is the one the player has, so keep
    # it as the fallback rather than stranding them with nothing to launch.
    fallback = None if root else _configured_legacy_root()

    info(f"{'Reinstalling' if root else 'Installing'} {edition['name']} "
         f"{entry['version']} — this downloads it from Microsoft with your "
         "own account …")
    # Every mirror the index lists, not just the first: they carry the same
    # package, so a truncated body from one is retryable on the next.
    url = entry["urls"][0]
    try:
        xodus.install(entry["urls"], dest, progress)
    except xodus.NotSignedIn:
        # Actionable, and only the caller can act: never fold this into the
        # fallback below, or the launcher quietly keeps starting the old build
        # instead of offering the sign-in that would install this one.
        raise
    except BolError as exc:
        if fallback is None:
            raise
        warn(f"Could not download {edition['name']} {entry['version']} "
             f"({exc}) — starting the copy already installed. It predates the "
             "switch to the Microsoft Store, so it stays on its own build "
             "until the download works.")
        return fallback
    root = _game_root(dest)
    if not root:
        die(f"Minecraft.Windows.exe missing after installing "
            f"{edition['name']} {entry['version']}.")
    _write_install_record(dest, edition, entry["version"], url)
    ok(f"{edition['name']} {entry['version']} installed")
    return root


def use_game_dir(folder):
    folder = Path(folder).expanduser().resolve()
    if not (folder / "Minecraft.Windows.exe").exists():
        cands = list(folder.rglob("Minecraft.Windows.exe"))
        if not cands:
            die(f"Minecraft.Windows.exe not found in {folder} (nor in "
                f"its subfolders). Choose an installed edition folder, "
                f"or use '① Minecraft edition'.")
        best = max(cands, key=lambda e: _vt(mc_version_str(e.parent) or "0"))
        folder = best.parent
        info(f"Minecraft found: {folder} "
             f"(version {mc_version_str(folder) or '?'})")
    if CONTENT.is_symlink() or CONTENT.exists():
        CONTENT.unlink() if CONTENT.is_symlink() else shutil.rmtree(CONTENT)
    CONTENT.symlink_to(folder)
    s = load_settings()
    s["game_dir"] = str(folder)
    # Remember what was selected so the picker and auto-select default to what
    # you last played. games/<edition>/<version>/ names both outright; a folder
    # from outside the managed tree names neither, and keeping the previous
    # choice there would silently reinstall over an imported copy.
    try:
        parts = folder.relative_to(GAMES.resolve()).parts
    except ValueError:
        parts = ()
    if len(parts) >= 2 and xodus.edition(parts[0]):
        s["mc_edition"] = parts[0]
        s["mc_version"] = parts[1]
    else:
        # An imported copy still reports its build, for display and bug reports.
        version = mc_version_str(folder)
        if version:
            s["mc_version"] = version
    save_settings(s)
    return folder


def mc_version_str(game_dir: Path):
    for nm in ("appxmanifest.xml", "AppxManifest.xml"):
        man = game_dir / nm
        if man.exists():
            m = re.search(r'Identity[^>]*Version="(\d+)\.(\d+)\.(\d+)\.\d+"',
                          man.read_text(errors="ignore"))
            if m:
                p = m.group(3)
                # Bedrock packs "<minor><patch>" into the Appx 3rd field, e.g.
                # 2004 -> "20.4", 3005 -> "30.5", 0301 -> "3.1" — split it back
                # so the result matches Mojang's version numbers (e.g. 1.26.20.4).
                if len(p) >= 3:
                    return f"{m.group(1)}.{m.group(2)}.{int(p[:2])}.{int(p[2:])}"
                return f"{m.group(1)}.{m.group(2)}.{int(p)}"
    return None


def _vt(v):
    try:
        return tuple(int(x) for x in v.split("."))
    except Exception:
        return (0,)


def _auto_selection(s):
    """The (edition, version) to install when the caller named neither.

    An unset or no-longer-listed version falls through to the newest build,
    which is what install_game() does with it.
    """
    want = (s.get("mc_edition") or "").strip()
    chosen = xodus.edition(want) if want else None
    if chosen is None:
        editions = list_editions(include_beta=False) or list_editions(True)
        if not editions:
            die("No Minecraft edition is available.")
        chosen = editions[0]
    return chosen, (s.get("mc_version") or "").strip() or None

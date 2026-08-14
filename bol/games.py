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
# Bedrock updates are infrequent and the delta check costs only package
# metadata, so re-check on the same cadence as the release-notes cache instead
# of on every launch.
_UPDATE_INTERVAL = 43200


def list_editions(include_beta=True):
    """The Minecraft editions available for installation.

    The Xbox CDN serves only the current build of a product id — there is no
    back catalogue — so this is Release and Preview rather than a list of
    Bedrock versions. The installed build number is read from the game's own
    manifest afterwards, which needs no authenticated round trip.
    """
    out = []
    for entry in xodus.list_editions():
        if entry["beta"] and not include_beta:
            continue
        entry["tag"] = entry["id"]
        root = _game_root(GAMES / entry["id"])
        entry["installed"] = mc_version_str(root) if root else None
        out.append(entry)
    return out


def _game_root(dest):
    """Folder of a complete installed build (exe + appxmanifest), else None
    (a bare exe with no manifest means a truncated install → reinstall)."""
    if not dest.exists():
        return None
    for exe in dest.rglob("Minecraft.Windows.exe"):
        if any((exe.parent / m).exists()
               for m in ("appxmanifest.xml", "AppxManifest.xml")):
            return exe.parent
    return None


def _install_record(dest):
    try:
        return json.loads(
            (Path(dest) / _INSTALL_METADATA).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}


def _write_install_record(dest, edition):
    record = {
        "schema": 1,
        "edition": edition["id"],
        "product": edition["product"],
        "xodus_rev": xodus.XODUS_REV,
        "checked": int(time.time()),
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


def _touch_update_check(dest, record):
    """Record a delta check that could not run, so it is retried on schedule
    rather than on every single launch."""
    if not record:
        return
    record = dict(record, checked=int(time.time()))
    target = Path(dest) / _INSTALL_METADATA
    staged = target.with_name("." + target.name + ".tmp")
    try:
        staged.write_text(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8")
        staged.replace(target)
    except OSError:
        pass
    finally:
        staged.unlink(missing_ok=True)


def _update_due(dest, record):
    checked = record.get("checked")
    if not isinstance(checked, int):
        return True
    # A clock moved backwards must not park the install on a stale build
    # forever, so treat a future timestamp as due rather than as fresh.
    return not (0 <= time.time() - checked < _UPDATE_INTERVAL)


def install_game(edition, progress=None, force=False):
    """Install or update one edition through Xodus.

    ``xodus-cli streaming`` is itself incremental and atomic: it compares the
    local segment hashes against the published package, fetches only what
    changed and commits its work package with a rename at the end. So this
    calls straight into it rather than staging a second copy — a staging dance
    would throw the delta away and re-download the whole package every time.
    """
    GAMES.mkdir(parents=True, exist_ok=True)
    dest = GAMES / edition["id"]
    root = _game_root(dest)
    record = _install_record(dest)
    # A scheduled delta check is a nicety; a missing, incomplete or mismatched
    # install is not. Only the first may be skipped when Xodus cannot run.
    optional = False
    if root and not force:
        if record.get("product") != edition["product"]:
            # The folder holds a different product than the one asked for;
            # never leave a mismatched tree in place under this edition's name.
            force = True
        elif not _update_due(dest, record):
            info(f"{edition['name']} already installed")
            return root
        else:
            optional = True

    info(f"{'Updating' if root else 'Installing'} {edition['name']} — this "
         "downloads it from Microsoft with your own account …")
    try:
        xodus.install(edition["product"], dest, progress)
    except BolError as exc:
        if not optional:
            raise
        # Being offline, signed out, or on a launcher whose downloader is not
        # published yet must never stand between the player and a game that is
        # already installed and complete.
        warn(f"Could not check {edition['name']} for updates ({exc}) — "
             "starting the installed build.")
        _touch_update_check(dest, record)
        return root
    root = _game_root(dest)
    if not root:
        die(f"Minecraft.Windows.exe missing after installing "
            f"{edition['name']}.")
    _write_install_record(dest, edition)
    version = mc_version_str(root)
    ok(f"{edition['name']} installed{f' ({version})' if version else ''}")
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
    # Remember which edition was selected so the picker and auto-select default
    # to the one you last played. games/<id>/ names it outright; a folder from
    # outside the managed tree has no edition, and keeping the previous choice
    # there would silently reinstall over an imported copy.
    try:
        chosen = folder.relative_to(GAMES.resolve()).parts[0]
    except ValueError:
        chosen = None
    if chosen and xodus.edition(chosen):
        s["mc_edition"] = chosen
    # mc_version is the build actually on disk, for display and bug reports.
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


def _auto_edition(s):
    want = (s.get("mc_edition") or "").strip()
    chosen = xodus.edition(want) if want else None
    if chosen:
        return dict(chosen, tag=chosen["id"])
    editions = list_editions(include_beta=False) or list_editions(True)
    if not editions:
        die("No Minecraft edition is available.")
    return editions[0]

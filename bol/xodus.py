"""bol.xodus — Minecraft acquisition through the Xodus CLI.

Xodus (https://github.com/xodus-gaming/xodus, GPL-3.0) is how the launcher gets
Minecraft: it signs in to the user's own Microsoft account, asks Microsoft's
licensing service for the title license — which carries the content key — then
streams the MSIXVC package from the official Xbox CDN and decrypts it on the
fly. It replaced a third-party repository that redistributed a DRM-stripped
copy of the game, so the account now has to actually own Minecraft.

Everything here shells out to the ``xodus-cli`` binary; no Xodus code is
linked. See third_party/xodus/README.md for the pin and the licensing note.
"""
# SPDX-License-Identifier: MIT

import hashlib
import json
import os
import pty
import re
import select
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import webview
from .archive import safe_extract_tar
from .config import (
    APP,
    CACHE,
    GDK_LINKS_URL,
    LEGACY_XODUS_KEYRING,
    MC_PRODUCTS,
    WINEGDK_PREBUILT_REPO,
    XODUS_ARCHIVE_SHA256,
    XODUS_BIN,
    XODUS_DIR,
    XODUS_HOME,
    XODUS_KEYRING,
    XODUS_REV,
)
from .log import BolError, info, ok, warn
from .util import _fetch_with_fallback, asset_url, download, gh_releases


class XodusError(BolError):
    """Xodus could not do what was asked."""


class NotSignedIn(XodusError):
    """No Microsoft account is linked to Xodus."""


class NotOwned(XodusError):
    """The linked account does not own the requested edition."""


# "Package was not found, is it owned by the user?" is what xodus prints when
# GetBasePackage refuses; it is by far the most common real-world failure, and
# it means something the user can act on rather than a bug. "not entitled to
# this content" is the same answer from the licensing service, which is the
# one a download started from a CDN URL gets -- and it arrives with exit
# code 0, so it used to be reported as "installed no game".
_NOT_OWNED = re.compile(
    r"package was not found|is it owned by the user|"
    r"not entitled to this content", re.I)
# The token failures are quoted from xodus-cli, typo included ("Unspported
# user token"), because that is what has to be matched.
_NO_CREDENTIALS = re.compile(
    r"unable to initialize credentials|invalid sts token|"
    r"no user token|not logged in|didn't log in|"
    r"un(sup|sp)ported (user )?token|failed to get exchange ms token", re.I)
# Out of room, said outright: "not enough free disk space on /home: need
# 2182632068 bytes, have 4096 bytes (files: 2182632068)". xodus-cli prints it
# and returns -- exiting 0 -- before a single game file is written.
_NO_ROOM = re.compile(
    r"not enough free disk space|failed to determine available space", re.I)
# The download racing its own package cache, which is what most reports of a
# failed install turn out to be (#217). xodus-cli streams the package through
# ".xodus-streaming-tmp.msixvc" in the destination and reads it back through a
# second handle to parse the package layout out of it -- but it counts the
# bytes tokio *accepted*, and tokio reports a file write accepted as soon as it
# has handed it to a blocking thread. A read dispatched to that same pool can
# overtake the write it is waiting for, find the file short, and take that for
# a corrupt package:
#   ok: Header(Io(Custom { kind: UnexpectedEof,
#                          error: "cache ended before cached_len" }))
# Measured against the pinned build, the cache claimed as much as 17 KiB more
# than the file held, on an idle disk with room to spare. A disk with no room
# left produces the same short read, so the two are told apart by the
# arithmetic below rather than by the message.
_CACHE_SHORT = re.compile(r"cache ended before cached_len", re.I)
# How many extra goes that race is worth. It is lost on a small fraction of
# the reads that land on a boundary, so a second attempt usually gets through
# and a third almost always does; past that the destination is telling us
# something else is wrong with it.
_CACHE_RACE_RETRIES = 3
# Microsoft licenses Store content to a device, and an account may hold ten of
# them at once. Until issue #198 every restart of the Flatpak claimed another,
# so an account can arrive here with its ten devices taken by a bug rather than
# by ten machines -- and Microsoft's own sentence says nothing about where they
# are given back.
_DEVICE_LIMIT = re.compile(r"device group is full", re.I)

# indicatif renders "  12.34 MiB/ 862.00 MiB"; the total bar is the one whose
# message is the launcher-visible stage rather than a file name.
_PROGRESS = re.compile(
    r"^\s*(?P<msg>\S[^\d]*?)\s+"
    r"(?P<done>[\d.]+)\s*(?P<du>[KMGT]?i?B)\s*/\s*"
    r"(?P<total>[\d.]+)\s*(?P<tu>[KMGT]?i?B)")
_UNITS = {"B": 1, "KiB": 1 << 10, "MiB": 1 << 20, "GiB": 1 << 30,
          "TiB": 1 << 40}
# Only the label is checked, and it has to be the *whole* label: a per-file
# or per-segment bar can be captioned "Downloading <name>", and a prefix
# match would take that for the aggregate bar too. Its own total is a single
# file, almost always small next to the whole package, so for one frame the
# launcher would report progress against the wrong denominator entirely --
# a percentage that has nothing to do with the real download -- until the
# next line carrying the real "Downloading" bar corrects it (#223).
_TOTAL_BAR = re.compile(r"^(initializing|downloading)\s*$", re.I)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
# ld.so, not Xodus: "…/xodus-cli: error while loading shared libraries:
# libwebkit2gtk-4.1.so.0: cannot open shared object file: No such file or
# directory" is all a host without WebKitGTK ever gets to print.
_LOADER_ERROR = re.compile(
    r"error while loading shared libraries:\s*([^:\s]+)")

# Failures that would otherwise be repeated by every refresh of a window.
_WARNED = {}


def edition(edition_id):
    """The MC_PRODUCTS entry for an edition id, or None."""
    return next((e for e in MC_PRODUCTS if e["id"] == edition_id), None)


def list_editions():
    """The editions Xodus can install."""
    return [dict(e) for e in MC_PRODUCTS]


def version_key(version):
    """Sort key for a Bedrock version string, tolerant of odd entries."""
    parts = []
    for piece in str(version).split("."):
        try:
            parts.append(int(piece))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _indexed_url(edition_entry, url):
    """Accept an indexed CDN URL, or return None with the reason logged.

    GetBasePackage only answers with the current build, so older ones come
    from a third-party index of CDN locations. The index carries no game data,
    but it decides what gets downloaded, so each entry has to look like what it
    claims: Microsoft's own asset host, this edition's content id, and an
    MSIXVC package.
    """
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    host = parsed.hostname or ""
    if not (host == "xboxlive.com" or host.endswith(".xboxlive.com")):
        return None
    if not parsed.path.lower().endswith(".msixvc"):
        return None
    if edition_entry["content_id"].lower() not in parsed.path.lower():
        return None
    return url


def version_catalogue(edition_id, ignore_cache=False):
    """Installable builds for an edition, newest first.

    Each entry is ``{"version": "1.26.44.3", "urls": [...]}``. Network failures
    fall back to the cached copy, so an offline launcher still lists what it
    listed last time instead of offering nothing.
    """
    entry = edition(edition_id)
    if entry is None:
        return []
    try:
        payload = _fetch_with_fallback(
            "gdk-links.json", GDK_LINKS_URL,
            ttl=0 if ignore_cache else 43200)
    except Exception as exc:
        raise XodusError(
            f"Could not read the list of Minecraft builds ({exc})."
        ) from exc
    channel = (payload or {}).get(entry["channel"])
    if not isinstance(channel, dict):
        raise XodusError(
            f"The list of Minecraft builds has no '{entry['channel']}' "
            "section.")

    out = []
    for version, urls in channel.items():
        if not isinstance(urls, list):
            continue
        usable = [u for u in (_indexed_url(entry, str(candidate))
                              for candidate in urls) if u]
        if usable:
            out.append({"version": str(version), "urls": usable})
    out.sort(key=lambda item: version_key(item["version"]), reverse=True)
    return out


# ---------------------------------------------------------------- binary


def cli_available():
    return XODUS_BIN.is_file() and os.access(XODUS_BIN, os.X_OK)


def ensure_cli():
    """Fetch + unpack the pinned xodus-cli into XODUS_DIR on first use.

    Mirrors fixups.ensure_openssl_xcurl_set(), except that a failure here is
    fatal: without xodus-cli there is no way to install the game at all.
    """
    marker = XODUS_DIR / ".rev"
    if cli_available() and marker.exists() and \
            marker.read_text().strip() == XODUS_REV:
        return XODUS_BIN

    asset = f"xodus-cli-{XODUS_REV}.tar.gz"
    # Unreleased candidates carry the reviewed asset beside the launcher, like
    # the engine archive, so local testing needs nothing published.
    anchors = []
    appimage = os.environ.get("APPIMAGE", "").strip()
    if appimage:
        anchors.append(Path(appimage).expanduser().resolve().parent)
    try:
        anchors.append(Path(sys.argv[0]).expanduser().resolve().parent)
    except (OSError, RuntimeError):
        pass
    tar = next((anchor / asset for anchor in anchors
                if (anchor / asset).is_file()), None)
    local_archive = tar is not None

    if not local_archive:
        try:
            rels = gh_releases(WINEGDK_PREBUILT_REPO, 30)
        except Exception as exc:
            raise XodusError(
                f"Could not look up the Xodus downloader ({exc}). Check the "
                "network connection and try again."
            ) from exc
        url = None
        for rel in rels or []:
            url, _name, _ = asset_url(rel, lambda n: n == asset)
            if url:
                break
        if not url:
            raise XodusError(
                f"The Xodus downloader '{asset}' has not been published yet.")
        tar = CACHE / asset
        if not tar.is_file():
            info("Downloading the Minecraft downloader (one-time) …")
            download(url, tar, "Xodus downloader")

    expected = XODUS_ARCHIVE_SHA256.strip().lower()
    if not expected:
        raise XodusError(
            "XODUS_ARCHIVE_SHA256 is unset, so the Xodus downloader cannot be "
            "verified. Publish a build with .github/workflows/build-xodus.yml "
            "and pin its SHA-256 in bol/config.py.")
    actual = hashlib.sha256(tar.read_bytes()).hexdigest()
    if actual != expected:
        # Never keep bytes that failed the pin: a cached bad archive would make
        # every later retry fail before download() could fetch a good one.
        if not local_archive:
            tar.unlink(missing_ok=True)
        raise XodusError(
            f"Xodus downloader SHA-256 mismatch (expected {expected}, got "
            f"{actual}); it was not installed.")

    XODUS_DIR.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".xodus-dl-", dir=XODUS_DIR.parent))
    try:
        with tarfile.open(tar) as archive:
            safe_extract_tar(archive, staging)
        binary = staging / "xodus-cli"
        if not binary.is_file():
            raise XodusError("The Xodus archive contains no xodus-cli binary.")
        binary.chmod(0o755)
        XODUS_DIR.mkdir(parents=True, exist_ok=True)
        for item in staging.iterdir():
            if item.is_file() and not item.is_symlink():
                target = XODUS_DIR / item.name
                fd, tmp_name = tempfile.mkstemp(
                    prefix="." + item.name + "-", dir=XODUS_DIR)
                os.close(fd)
                tmp = Path(tmp_name)
                try:
                    shutil.copy2(item, tmp)
                    tmp.replace(target)
                finally:
                    tmp.unlink(missing_ok=True)
        marker.write_text(XODUS_REV + "\n", encoding="utf-8")
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    ok(f"Minecraft downloader ready (xodus {XODUS_REV})")
    return XODUS_BIN


# ---------------------------------------------------------------- account


def home():
    """The HOME xodus-cli is run with, created if it is not there yet.

    Xodus writes its keyring to ``$HOME/.xodus-keyring.ron``, and $HOME is the
    wrong place for it. Inside the Flatpak that directory is a tmpfs the
    sandbox throws away on exit, so the sign-in lasted exactly as long as the
    window it was made in (issue #198). Losing it is worse than a sign-out:
    every Xodus command that needs an identity calls provision_device() when
    the keyring has no device credentials, so each restart claimed *another*
    Microsoft Store device, and an account may hold ten before the licensing
    service stops handing out the game — "Device group is full, please remove
    a device and try again", with the remedy on a web page rather than here.

    A directory of the launcher's own avoids all of it: it is inside DATA, so
    it persists exactly as long as the installed game does, in every packaging,
    and it needs no Flatpak permission because that is already the app's own
    storage. Nothing but Xodus is put in it — the user's real home is neither
    read nor written.
    """
    try:
        XODUS_HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        # Once per run: signed_in() asks for this directory every time a
        # window refreshes, and one that cannot be created stays that way.
        if not _WARNED.get("home"):
            _WARNED["home"] = True
            warn(f"Could not create {XODUS_HOME} for the Microsoft Store "
                 f"sign-in ({exc}).")
        return XODUS_HOME
    _adopt_legacy_keyring()
    return XODUS_HOME


def _adopt_legacy_keyring():
    """Take along a keyring written before the launcher owned Xodus's home.

    Anyone who signed in before this release has their tokens in
    ``$HOME/.xodus-keyring.ron``, and starting that session over is not free:
    it spends one of the account's ten Store devices. So the file comes with
    them. It is copied rather than moved, like every other migration here, so
    an older launcher on the same machine keeps the session it wrote; logout()
    removes the copy left behind, so unlinking the account still leaves no
    live tokens in the user's home directory.

    Costs two stat() calls and touches nothing once there is a keyring in the
    new place, which is what lets signed_in() ask for it on every refresh.
    """
    if XODUS_KEYRING.exists() or not LEGACY_XODUS_KEYRING.is_file():
        return False
    try:
        blob = LEGACY_XODUS_KEYRING.read_bytes()
        XODUS_HOME.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, staged = tempfile.mkstemp(prefix=".keyring-", dir=XODUS_HOME)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(blob)
            # Xodus creates it 0600 and the tokens are the account; a
            # world-readable copy would be the launcher's doing, not Xodus's.
            os.chmod(staged, 0o600)
            os.replace(staged, XODUS_KEYRING)
        finally:
            Path(staged).unlink(missing_ok=True)
    except OSError as exc:
        # Once per run, like home(): this is asked again at every refresh.
        if not _WARNED.get("adopt"):
            _WARNED["adopt"] = True
            warn(f"Could not copy the Microsoft Store sign-in into "
                 f"{XODUS_HOME} ({exc}); you may have to sign in again.")
        return False
    info("Kept the existing Microsoft Store sign-in "
         f"({LEGACY_XODUS_KEYRING} → {XODUS_KEYRING}).")
    return True


def signed_in():
    """True when Xodus holds a usable Microsoft *user* session.

    Xodus is built with --features key-chain-file, so its tokens live in a
    single RON file instead of a D-Bus secret service (which does not exist in
    a Steam Deck Game Mode session or inside a Flatpak sandbox); home() says
    where that file is now kept. This is also where a keyring left in the
    user's own home by an earlier release is taken over, because it is the
    question every window asks before it offers anyone a sign-in.

    The file existing proves nothing: every command that needs an identity
    provisions device credentials first, which creates the keyring with only a
    'device-tokens' entry. Downloading needs the *user* token that
    `xodus-cli login` stores under 'user-tokens' — without it the download dies
    deep inside Xodus on a missing token instead of asking anyone to sign in.
    """
    _adopt_legacy_keyring()
    try:
        blob = XODUS_KEYRING.read_bytes()
    except OSError:
        return False
    return b"user-tokens" in blob


def login():
    """Run the interactive Xodus sign-in.

    This is a *separate* account link from bol.auth's device-code flow, which
    stays as-is for the in-game sign-in: Xodus needs a device-bound legacy RPS
    token to talk to the licensing service, which a device-code OAuth token
    cannot stand in for. Opens Xodus's own webview window, so it needs a
    display and libwebkit2gtk-4.1 -- from the host, or from the runtime
    bol.webview installs where the host has none.
    """
    binary = ensure_cli()
    info("Sign in to the Microsoft account that owns Minecraft …")
    proc = subprocess.run([str(binary), "login"], env=_env(binary),
                          capture_output=True, text=True)
    if proc.returncode != 0 or not signed_in():
        output = (proc.stderr or proc.stdout or "").strip()
        detail = output.splitlines()
        raise XodusError(
            _loader_failure(output) or
            "Microsoft sign-in for the Minecraft download did not complete"
            + (f": {detail[-1]}" if detail else ".")
        )
    ok("Microsoft account linked for the Minecraft download.")
    return True


def logout(device=False):
    binary = ensure_cli()
    cmd = [str(binary), "logout"] + (["--device"] if device else [])
    subprocess.run(cmd, env=_env(binary), capture_output=True, text=True)
    # xodus-cli only knows about the keyring it was pointed at. The one
    # _adopt_legacy_keyring() copied from is still lying in the user's home
    # with live tokens in it, and "unlink this account" has to mean that one
    # too.
    if XODUS_KEYRING.exists():
        try:
            LEGACY_XODUS_KEYRING.unlink(missing_ok=True)
        except OSError:
            pass


def _loader_failure(text):
    """The message to show when xodus-cli died in the dynamic loader.

    _env() installs the bundled WebKitGTK before that can happen, so reaching
    here means the library it found was unusable -- report the missing library
    rather than "No such file or directory", which is what the loader says and
    what issue #184 is about.
    """
    match = _LOADER_ERROR.search(text or "")
    if not match:
        return None
    return webview.missing_message(
        "The Minecraft downloader could not start: it needs "
        f"{match.group(1)}, which this system does not have.")


def _env(binary=None):
    """The environment xodus-cli runs in.

    xodus-cli cannot start at all without WebKitGTK -- the login webview is
    linked into every subcommand -- so hosts that ship none get the bundled
    runtime added here rather than at the sign-in alone.
    """
    env = os.environ.copy()
    # Xodus writes its file keyring under $HOME, so $HOME is what decides
    # whether the sign-in outlives the window it was made in: see home().
    env["HOME"] = str(home())
    webview.apply(binary if binary is not None else XODUS_BIN, env)
    return env


# ---------------------------------------------------------------- install

# What xodus-cli names the package it keeps beside the game once a download
# completed. It is not a leftover: the segments a GDK title flags
# KEEP_ENCRYPTED_ON_DISK -- the game executable -- are only ever stored in
# here, so it is read again at every launch as well as by the next delta.
PACKAGE_CACHE = ".xodus-streaming.msixvc"
_PACKAGE_CACHE_TMP = ".xodus-streaming-tmp*.msixvc"


def _bytes(value, unit):
    try:
        return int(float(value) * _UNITS.get(unit, 1))
    except (TypeError, ValueError):
        return 0


def game_root(dest):
    """Folder of a complete build under ``dest``, else None.

    This is the shape ``xodus-cli streaming`` leaves behind, so it lives here
    rather than in the caller: a bare exe with no manifest beside it is a
    truncated install, not a build.
    """
    dest = Path(dest)
    if not dest.exists():
        return None
    for exe in dest.rglob("Minecraft.Windows.exe"):
        if any((exe.parent / m).exists()
               for m in ("appxmanifest.xml", "AppxManifest.xml")):
            return exe.parent
    return None


def has_package_cache(directory):
    """Whether the package a build is decrypted from sits in ``directory``.

    ``xodus-cli run`` reads the cache and the ciphertext segments out of the
    single directory it is handed, so "beside the game" is literal: the
    launcher cannot point it at a copy kept anywhere else.
    """
    try:
        return (Path(directory) / PACKAGE_CACHE).is_file()
    except OSError:
        return False


def lost_package_cache(game_dir):
    """Whether ``game_dir`` holds a build it can no longer decrypt.

    A Store build keeps its executable as ciphertext and the package beside it
    is the only thing that can turn it back into a program, at every launch --
    so a directory that lost that file looks like a complete install and is
    not one. Until issue #216 nothing acted on that: the folder still had an
    exe and a manifest, so it counted as installed, the download was skipped
    as unnecessary and the launch died on the missing package every time. The
    only way out was to delete the build by hand.

    A build from before the move to the Store carries a plaintext executable
    and needs no package at all, so it is never reported here.
    """
    game_dir = Path(game_dir)
    exe = game_dir / "Minecraft.Windows.exe"
    if not exe.is_file() or not exe_is_encrypted(exe):
        return False
    return not has_package_cache(game_dir)


def _drop_cache(dest):
    """Delete the package cache xodus-cli left behind in ``dest``.

    xodus-cli caches the encrypted package beside the game -- as
    ".xodus-streaming-tmp.msixvc" while a download runs, renamed to
    ".xodus-streaming.msixvc" once one completed -- and re-opens it on the next
    run. A short one is therefore permanent: every retry seeks into it, reads
    past its end and panics ("cache ended before cached_len"), or concludes the
    delta is empty and installs nothing. Deleting it is what makes a retry an
    actual retry rather than three replays of the same failure.

    The completed cache is not spare data -- ``xodus-cli run`` decrypts the
    keep_encrypted segments out of it at every launch -- so it only goes when
    ``dest`` holds no playable build for it to belong to.
    """
    dest = Path(dest)
    stale = list(dest.glob(_PACKAGE_CACHE_TMP))
    if game_root(dest) is None:
        stale += list(dest.glob(PACKAGE_CACHE))
    for path in stale:
        try:
            path.unlink()
        except OSError:
            pass


_DEVICE_LIMIT_MESSAGE = (
    "Microsoft will not license Minecraft to this machine: the account has "
    "reached its limit of ten Microsoft Store download devices. Remove the "
    "ones you no longer use at https://account.microsoft.com/devices/content, "
    "then try again.")


def _human(size):
    """A size a sentence can carry."""
    value = float(size)
    for unit in ("B", "KiB", "MiB"):
        if value < 1024:
            return f"{value:.0f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def _free_space(path):
    """Bytes free where ``path`` lives, or None when that cannot be read."""
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return None


def _package_size(sources):
    """What the CDN says the package weighs, or 0 when it will not say.

    One HEAD against the same URL xodus-cli is about to stream from, so the
    answer describes the exact build being installed rather than an average.
    Anything that goes wrong -- a mirror that refuses HEAD, no network yet, a
    product id rather than a URL -- returns 0 and the caller simply does not
    check, because a size we could not read is not a reason to refuse an
    install that might well fit.
    """
    for source in sources:
        url = str(source)
        if not url.lower().startswith(("http://", "https://")):
            continue
        try:
            request = urllib.request.Request(
                url, method="HEAD", headers={"User-Agent": APP})
            with urllib.request.urlopen(request, timeout=15) as response:
                length = int(response.headers.get("Content-Length") or 0)
        except (OSError, ValueError):
            continue
        if length > 0:
            return length
    return 0


# What a Bedrock download needs when the CDN would not say how big it is: no
# build has ever come close to being this small, so a destination with less
# than this left is out of room whatever the exact figure turns out to be.
_MIN_ROOM = 1 << 30


def _room_needed(package):
    """What a first install of a package that size takes in ``dest``.

    The build decrypted out of the package is about the size of the package
    itself, and the prefix xodus-cli caches beside it is not free either: for
    1.26.44.3 the CDN reports 2.32 GiB and what stays on disk is 2.32 GiB of
    game plus a 187 MiB cache. Measuring the package alone would wave through
    a disk that then fills up mid-download, which is the failure this check
    exists to prevent, so the figure carries the cache and a little slack.
    """
    if not package:
        return 0
    return package + max(package // 8, 256 << 20)


def _short_of_room(needed, free):
    """Whether ``free`` bytes cannot hold this download."""
    if free is None:
        return False
    return free < (needed or _MIN_ROOM)


def _no_room_message(dest, needed, free):
    """Say how much room the download wants and how much there is."""
    where = f"There is not enough room to download Minecraft into {dest}"
    if needed:
        return (
            f"{where}: it needs about {_human(needed)} free — the "
            f"package is streamed through a cache beside the game and the "
            f"build is decrypted out of it — and "
            + (f"only {_human(free)} is left. " if free is not None else
               "there is less than that. ")
            + "Free up some space, or install to another drive, and try "
              "again.")
    return (
        f"{where}"
        + (f": only {_human(free)} is left there. " if free is not None
           else ". ")
        + "Free up some space, or install to another drive, and try again.")


def _cache_short_clause(dest, needed, free):
    """Why a download died on its own cache, without the Rust wording."""
    short = (f"the package cache in {dest} read back shorter than what had "
             "been written to it")
    if not _short_of_room(needed, free):
        # The downloader outran its own write rather than ran out of disk, so
        # say which of the two it was; the disk is the first thing anyone
        # suspects, and here it is the one thing that was fine.
        return short + (
            f" — the downloader read it back before the write landed, not for "
            f"want of room ({_human(free)} is free there)"
            if free is not None else
            " — the downloader read it back before the write landed")
    room = f"{_human(free)} free"
    if needed:
        room += f", and the download needs about {_human(needed)}"
    return f"{short}, which is what a disk with no room left does ({room})"


def _raise_unretryable(text, dest=None, needed=0):
    """Raise for the download failures another mirror cannot fix."""
    if _DEVICE_LIMIT.search(text):
        raise XodusError(_DEVICE_LIMIT_MESSAGE)
    if _NOT_OWNED.search(text):
        raise NotOwned(
            "The linked Microsoft account does not own this edition of "
            "Minecraft. Buy or redeem it on the same account, then try again.")
    if _NO_CREDENTIALS.search(text):
        raise NotSignedIn(
            "The Microsoft session for the download expired. Sign in again.")
    if dest is not None:
        free = _free_space(dest)
        # xodus-cli measured the room itself, so this one is settled.
        if _NO_ROOM.search(text):
            raise XodusError(_no_room_message(dest, needed, free))
        # A short cache is what a full disk looks like from inside the parse,
        # and only the arithmetic can tell that from a write that failed for
        # some other reason -- so it is fatal only when the room really is
        # missing, and otherwise stays worth asking another mirror about.
        if _CACHE_SHORT.search(text) and _short_of_room(needed, free):
            raise XodusError(_no_room_message(dest, needed, free))
    loader = _loader_failure(text)
    if loader:
        raise XodusError(loader)


def install(product, dest: Path, progress=None):
    """Download + decrypt an edition into ``dest``.

    ``product`` is a Store product id, a CDN package URL, or the list of mirror
    URLs the index carries for one build. The mirrors are the same package on
    different Microsoft asset hosts, so a body one of them cuts short is worth
    asking the next one for.

    ``xodus-cli streaming`` is incremental and commits atomically on its own:
    it compares the local segment hashes against the remote package, fetches
    only the changed files, and renames its work package into place at the end.
    So there is deliberately no staging/rollback dance around it here — adding
    one would defeat the delta and re-download the whole 2+ GiB every time.
    What it does need is _drop_cache(): the delta is a shortcut only while the
    cache it reads belongs to a build that is really there.

    A fresh install is also measured against the free space first. Everything
    xodus-cli needs lands in ``dest``: the package it streams through, and the
    build decrypted out of it -- 2.32 GiB of game beside a 187 MiB cache for
    1.26.44.3 -- so the package's own Content-Length is a fair figure for
    both, and it is one HEAD away. Without that check a disk with a few
    hundred MiB left produced two failures that named neither the disk nor the
    room: the cache write was refused mid-parse and xodus-cli panicked on the
    short read, and once the cache was dropped the next attempt got as far as
    xodus-cli's own space check, which prints its verdict and exits 0.

    A short cache read on a disk that has the room is not the disk at all but
    xodus-cli racing its own write (see _CACHE_SHORT), and that is worth
    simply running again: a fresh attempt re-reads only the prefix the parse
    needs rather than the whole build.
    """
    binary = ensure_cli()
    if not signed_in():
        raise NotSignedIn(
            "Minecraft is downloaded from Microsoft with your own account, so "
            "you have to sign in before it can be installed.")
    sources = ([product] if isinstance(product, str)
               else [str(source) for source in product])
    if not sources:
        raise XodusError("This Minecraft build has no download location.")
    dest.mkdir(parents=True, exist_ok=True)

    # A package cache with no build beside it is what an attempt that never
    # finished leaves behind, and xodus-cli resumes from it: nothing here can
    # be resumed -- it truncates its work cache at every start -- so all it
    # can still do is make the delta look empty, which is a download that
    # "succeeds" and installs nothing at all.
    _drop_cache(dest)

    # Only for a download of the whole package: an update to a build already
    # here fetches a delta of unknown size, and refusing that on the whole
    # package's figure would block updates that fit perfectly well. A build
    # that lost its package is not an update -- there is no cache left to
    # delta against, so the entire 2+ GiB comes down again (issue #216).
    needed = 0
    if game_root(dest) is None or lost_package_cache(dest):
        needed = _room_needed(_package_size(sources))
        free = _free_space(dest)
        if _short_of_room(needed, free):
            raise XodusError(_no_room_message(dest, needed, free))

    failure = ""
    # Every mirror once, plus a few goes at whichever one loses the cache
    # race, which is the same package either way.
    plan = list(sources)
    races_left = _CACHE_RACE_RETRIES
    while plan:
        source = plan.pop(0)
        cmd = [str(binary), "streaming", source, str(dest)]
        code, tail = _run_streaming(cmd, progress)
        root = game_root(dest)
        if code == 0 and root is not None and has_package_cache(dest):
            return dest
        _drop_cache(dest)
        text = "\n".join(tail)
        line = _failure_line(tail)
        # Whatever it exited with. Most of the paths that end a download early
        # -- no licence, no room, an expired session -- print their reason and
        # then exit 0 anyway, so classifying only the non-zero exits reported
        # an account that does not own Minecraft as "installed no game".
        _raise_unretryable(text, dest, needed)
        raced = False
        if _CACHE_SHORT.search(text):
            free = _free_space(dest)
            line = _cache_short_clause(dest, needed, free)
            raced = not _short_of_room(needed, free)
        # Xodus also exits 0 having installed nothing at all, when the cache it
        # resumed from makes the delta look empty. Treat that as the failure it
        # is, or the caller starts a game directory that was never written.
        if code == 0 and root is None:
            failure = ("The Minecraft download installed no game"
                       + (f": {line}" if line else
                          " and printed no reason for it."))
        elif code == 0:
            # Every path that ends the download early -- no licence, no disk
            # space -- returns before xodus-cli renames its package into
            # place, and still exits 0. With an older build already unpacked
            # here that used to read as a finished install, and the game only
            # failed hours later, at launch, on the package that was never
            # written. It is not installed until it can be decrypted.
            failure = ("The Minecraft download did not complete"
                       + (f": {line}" if line else
                          ", so the game cannot be decrypted."))
        else:
            failure = "The Minecraft download failed" + (
                f": {line}" if line else ".")
        if raced and races_left:
            races_left -= 1
            plan.insert(0, source)
            warn(f"{failure} Starting it again …")
        elif plan:
            warn(f"{failure} Retrying from another Microsoft mirror …")
    raise XodusError(failure)


def _failure_line(tail):
    """The line worth showing the user out of what the download printed.

    A Rust panic ends with "note: run with RUST_BACKTRACE=1", so taking the
    last line reports the least informative one and hides the actual cause on
    the line above.
    """
    lines = [line.strip() for line in tail if line.strip()]
    for index, line in enumerate(lines):
        if "panicked at" in line:
            message = next((later for later in lines[index + 1:]
                            if not later.startswith("note:")), "")
            return message or line
    return next((line for line in reversed(lines)
                 if not line.startswith("note:")), "")


def _drawable_term(env):
    """Name a terminal indicatif is willing to draw its bars on.

    A pty is necessary but not sufficient: indicatif also stays silent when
    TERM says the terminal cannot be drawn on, and a launcher started from a
    desktop entry, Steam or Game Mode passes no TERM at all. That is the
    ordinary case, so the whole multi-gigabyte download used to arrive without
    a single progress line and the launcher could only sweep a busy bar.

    A TERM the session already set is left alone -- it describes the terminal
    someone is actually watching.
    """
    if (env.get("TERM") or "").strip().lower() in ("", "dumb", "unknown"):
        env["TERM"] = "xterm-256color"
    return env


def _run_streaming(cmd, progress=None):
    """Run xodus-cli and translate its progress bars into progress(done, total).

    indicatif hides its bars entirely when stderr is not a terminal, so the
    child gets a pty; without one there would be no progress to report at all.
    """
    tail = []
    env = _drawable_term(_env(cmd[0]))
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(cmd, stdout=slave, stderr=slave, stdin=slave,
                                env=env, close_fds=True)
    except OSError as exc:
        os.close(master)
        os.close(slave)
        raise XodusError(f"Could not start the Minecraft downloader: {exc}") \
            from exc
    os.close(slave)
    buffer = ""
    try:
        while True:
            ready, _, _ = select.select([master], [], [], 0.5)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", "replace")
                # indicatif redraws with \r and ANSI cursor moves rather than
                # newlines, so split on both.
                parts = re.split(r"[\r\n]", buffer)
                buffer = parts.pop()
                for line in parts:
                    _consume(_ANSI.sub("", line), tail, progress)
            elif proc.poll() is not None:
                break
    finally:
        os.close(master)
        proc.wait()
    if buffer:
        _consume(_ANSI.sub("", buffer), tail, progress)
    return proc.returncode, tail


def _consume(line, tail, progress):
    line = line.rstrip()
    if not line:
        return
    match = _PROGRESS.match(line)
    if match:
        # Only the total bar drives the launcher's progress; the per-file bars
        # would make it jump backwards on every new file.
        if progress and _TOTAL_BAR.match(match.group("msg").strip()):
            done = _bytes(match.group("done"), match.group("du"))
            total = _bytes(match.group("total"), match.group("tu"))
            if total:
                progress(min(done, total), total)
        return
    tail.append(line)
    del tail[:-40]


# ---------------------------------------------------------------- launching


def exe_is_encrypted(exe: Path):
    """True when the game executable is still ciphertext on disk.

    Xodus keeps the segments the package flags KEEP_ENCRYPTED_ON_DISK — the
    game executable on GDK titles — encrypted at rest, exactly like Windows,
    and decrypts them into anonymous memory at launch. Whether that applies to
    a given build is a property of the package, so detect it instead of
    assuming: a plaintext PE starts with "MZ".
    """
    try:
        with open(exe, "rb") as stream:
            return stream.read(2) != b"MZ"
    except OSError:
        return False


def staging_dir(environ=None, info_path=Path("/.flatpak-info")):
    """Return the RAM-backed directory the decrypted image is staged in.

    Wine opens that file from inside the Steam Linux Runtime container, so the
    directory has to be one the container can reach. /dev/shm is, except under
    Flatpak: pressure-vessel builds the container as a *new* app instance and
    says so itself -- "/dev/shm not shared between app instances
    (flatpak#4214)" -- so the image staged by the launcher was simply not
    there, the loader fell through to the ciphertext on disk, and the game
    died on "ShellExecuteEx failed: File not found" (issue #193).

    $XDG_RUNTIME_DIR is the way through: Flatpak binds one tmpfs per
    application at that same path in every instance of it, so a file written
    here is readable -- and unlinkable -- inside the container. It is RAM like
    /dev/shm and mode 0700, so nothing about the handoff weakens.
    """
    source = os.environ if environ is None else environ
    if source.get("FLATPAK_ID") or Path(info_path).is_file():
        runtime = (source.get("XDG_RUNTIME_DIR") or "").strip()
        if runtime:
            return Path(runtime) / APP
    return Path("/dev/shm")


def _sweep_staged_images(older_than=60, directories=None):
    """Drop staged images a previous launch left behind.

    The loader unlinks its copy within milliseconds of opening it, and the
    wrapper execs and never returns, so anything still named here is from a
    launch that died before the image was mapped. Each one is the size of the
    game executable and both staging directories are RAM, so they cannot be
    left to accumulate. Sweep every location the launcher stages in, not just
    today's: which one that is depends on how the launcher was installed.
    """
    cutoff = time.time() - older_than
    if directories is None:
        directories = [staging_dir(), Path("/dev/shm")]
    leftovers = []
    for directory in dict.fromkeys(Path(d) for d in directories):
        try:
            leftovers += list(directory.glob("bol-*"))
        except OSError:
            continue
    for path in leftovers:
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


_WRAPPER = '''\
#!/usr/bin/env python3
"""Generated by BedrockOnLinux — do not edit; it is rewritten on every launch.

`xodus-cli run` decrypts the game executable into an anonymous memfd, clears
FD_CLOEXEC on it, publishes it through WINE_DLL_FILE_MAP and then execs this
script. Standing here, in between, is what lets the launcher keep its own
launch command -- and what converts the map.

The game runs inside the Steam Linux Runtime container, where a descriptor
number means nothing: Wine reported "sendmsg: Bad file descriptor" and died.
So each descriptor is copied into a private file the container can open and
the map hands over that path instead. STAGE_DIR is RAM -- /dev/shm, or the
application's $XDG_RUNTIME_DIR under Flatpak, where /dev/shm is not shared
with the container (see bol.xodus.staging_dir) -- the copy is created 0600,
and the loader unlinks it the instant it opens it, so the decrypted image
carries a name for milliseconds and never reaches durable storage.
"""
import json
import os
import shutil
import sys
import tempfile

ARGV = json.loads({argv!r})
LAUNCHER_PATH = {launcher_path!r}
EXE_NAME = {exe_name!r}
STAGE_DIR = {stage_dir!r}
# What the game's environment looked like before it was adjusted for
# `xodus-cli run`: the bundled WebKitGTK runtime, where the host has none, and
# the Xodus home the licence is read from. Neither belongs to the game. Wine
# and the Steam Linux Runtime bring their own libraries, so a stray
# LD_LIBRARY_PATH would put ours in front of them, and both keep state of
# their own under $HOME.
WEBVIEW_ENV = json.loads({webview_env!r})

ENTRIES = []
for entry in (os.environ.get("WINE_DLL_FILE_MAP") or "").split("|"):
    fd_text, _, mapped = entry.partition(":")
    if fd_text.isdigit() and mapped:
        ENTRIES.append((int(fd_text), mapped))

# Pick the game executable out of the map by name. Naming it to xodus-cli
# instead would mean guessing how the package spells its own segment keys, and
# a wrong guess is fatal there ("Could not find .exe"); here the name is known
# for certain. chr(92) is the NT separator -- a literal backslash would have to
# survive both this template and the generated file.
target = next((e for e in ENTRIES
               if e[1].lower().rsplit(chr(92), 1)[-1] == EXE_NAME.lower()),
              None)
nt_name = target[1] if target else (sys.argv[1] if len(sys.argv) > 1 else None)
if not nt_name:
    sys.exit("no NT executable name was passed by xodus-cli run")


def stage(fd_number):
    """Copy an inherited descriptor into a file the container can open."""
    os.makedirs(STAGE_DIR, mode=0o700, exist_ok=True)
    handle, path = tempfile.mkstemp(prefix="bol-", dir=STAGE_DIR)
    try:
        os.fchmod(handle, 0o600)
        os.lseek(fd_number, 0, os.SEEK_SET)
        with open(fd_number, "rb", closefd=False) as source, \
                open(handle, "wb", closefd=False) as target:
            shutil.copyfileobj(source, target, length=1 << 22)
    finally:
        os.close(handle)
    return path


staged = {{}}
try:
    for fd_number, mapped in ENTRIES:
        staged[mapped] = stage(fd_number)
except OSError as exc:
    for path in staged.values():
        try:
            os.unlink(path)
        except OSError:
            pass
    sys.exit("could not stage the decrypted game in %s: %s" % (STAGE_DIR, exc))

# Raise the PE stack reserve exactly as the plaintext path does (issue #27).
# The loader reads the field when it maps the image, so it has to happen
# before the exec below. Never fatal: a game that starts with a 1 MB stack
# still beats one that does not start at all.
try:
    sys.path.insert(0, LAUNCHER_PATH)
    from bol.fixups import _raise_stack_reserve

    if nt_name in staged:
        with open(staged[nt_name], "r+b") as image:
            _raise_stack_reserve(image.fileno())
except Exception as exc:                                  # noqa: BLE001
    print("bol: could not raise the stack reserve: %s" % exc, file=sys.stderr)

if staged:
    os.environ["WINE_DLL_FILE_MAP"] = "|".join(
        "%s:%s" % (path, mapped) for mapped, path in staged.items())
for name, value in WEBVIEW_ENV.items():
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
os.execvp(ARGV[0], ARGV + [nt_name])
'''


def wrap_encrypted_launch(argv, game_dir: Path, work_dir: Path,
                          launcher_path=None, env=None):
    """Turn a launch command into one that can start an encrypted executable.

    The returned command runs ``xodus-cli run`` outermost. It holds the license
    and the XVD decryption, which live in Xodus's Rust crates and are not
    reimplementable here, and it hands the plaintext to Wine as a descriptor
    rather than a file. ``argv``'s last element is the executable path, which
    the wrapper replaces with the NT name Xodus assigns.

    ``env`` is the environment the game will be started with. Since xodus-cli
    is the outermost process, a host without WebKitGTK needs the bundled
    runtime in *that* environment -- and so does the Xodus home holding the
    licence. Both are added here and taken back out by the wrapper, one exec
    before the game.
    """
    if not signed_in():
        # The licence for an encrypted build is fetched at every launch, so a
        # lost or unlinked session does not fail at the next download -- it
        # fails here, and it used to do it as a Rust panic on a missing
        # keyring entry (issue #198). Name what is missing instead: the
        # launcher has a button for exactly this.
        raise NotSignedIn(
            "Minecraft is decrypted with the Microsoft account that owns it, "
            "so it cannot start until that account is linked again.")
    if not has_package_cache(game_dir):
        # The other half of the same story: the account is linked, but the
        # package the executable is decrypted *from* is not there. xodus-cli
        # opens it unconditionally and unwraps the error, so what reached the
        # player was a Rust panic naming a line of Rust
        # ("run.rs:133 ... Os { code: 2, kind: NotFound }") and a launcher
        # that died with it. Say which file is missing, and that only a fresh
        # download brings it back: the segments it holds exist nowhere else
        # on disk.
        # Name something that exists, too: this used to send the player to
        # "Install / Update", which is a CLI verb the launcher window has no
        # tab for, and there was no way to reinstall a build from it anyway
        # (issue #216). PLAY is the answer now — a build that lost its
        # package no longer counts as installed, so setup fetches it again.
        raise XodusError(
            f"Minecraft's encrypted package ({PACKAGE_CACHE}) is missing "
            f"from {game_dir}. The game executable there is ciphertext and "
            "that package is what decrypts it, so this build cannot start "
            "until it is downloaded again. Open the launcher and press "
            "PLAY: it downloads a build whose package went missing.")
    binary = ensure_cli()
    _sweep_staged_images()
    restore = webview.apply(binary, env) if env is not None else None
    if env is not None:
        # `xodus-cli run` reads the licence out of the same keyring the
        # download signed in to, so it needs the launcher's Xodus home too --
        # and the game below must not inherit it: Wine, umu and the Steam
        # runtime all keep state of their own under $HOME.
        restore = dict(restore or {})
        restore["HOME"] = env.get("HOME")
        env["HOME"] = str(home())
    work_dir.mkdir(parents=True, exist_ok=True)
    wrapper = work_dir / "xodus-launch-wrapper.py"
    if launcher_path is None:
        launcher_path = str(Path(__file__).resolve().parent.parent)
    stage_dir = staging_dir()
    if not stage_dir.is_dir():
        # Only ever created, never adjusted: /dev/shm is the system's, and the
        # one under $XDG_RUNTIME_DIR is born 0700 like everything else there.
        # Not fatal either -- the wrapper creates it too, and it is the one
        # whose failure has to stop the launch, holding the plaintext.
        try:
            stage_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            warn(f"Could not create {stage_dir} for the decrypted game: {exc}")
    wrapper.write_text(
        _WRAPPER.format(argv=json.dumps(list(argv[:-1])),
                        launcher_path=launcher_path,
                        exe_name=Path(argv[-1]).name,
                        stage_dir=str(stage_dir),
                        webview_env=json.dumps(restore or {})),
        encoding="utf-8")
    wrapper.chmod(0o755)
    return [str(binary), "run", str(game_dir), str(wrapper)]

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
from pathlib import Path

from .archive import safe_extract_tar
from .config import (
    CACHE,
    GDK_LINKS_URL,
    MC_PRODUCTS,
    WINEGDK_PREBUILT_REPO,
    XODUS_ARCHIVE_SHA256,
    XODUS_BIN,
    XODUS_DIR,
    XODUS_KEYRING,
    XODUS_REV,
)
from .log import BolError, info, ok
from .util import _fetch_with_fallback, asset_url, download, gh_releases


class XodusError(BolError):
    """Xodus could not do what was asked."""


class NotSignedIn(XodusError):
    """No Microsoft account is linked to Xodus."""


class NotOwned(XodusError):
    """The linked account does not own the requested edition."""


# "Package was not found, is it owned by the user?" is what xodus prints when
# GetBasePackage refuses; it is by far the most common real-world failure, and
# it means something the user can act on rather than a bug.
_NOT_OWNED = re.compile(r"package was not found|is it owned by the user", re.I)
_NO_CREDENTIALS = re.compile(
    r"unable to initialize credentials|invalid sts token|"
    r"no user token|not logged in|didn't log in", re.I)

# indicatif renders "  12.34 MiB/ 862.00 MiB"; the total bar is the one whose
# message is the launcher-visible stage rather than a file name.
_PROGRESS = re.compile(
    r"^\s*(?P<msg>\S[^\d]*?)\s+"
    r"(?P<done>[\d.]+)\s*(?P<du>[KMGT]?i?B)\s*/\s*"
    r"(?P<total>[\d.]+)\s*(?P<tu>[KMGT]?i?B)")
_UNITS = {"B": 1, "KiB": 1 << 10, "MiB": 1 << 20, "GiB": 1 << 30,
          "TiB": 1 << 40}
_TOTAL_BAR = re.compile(r"^(initializing|downloading)", re.I)
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


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


def signed_in():
    """True when Xodus holds a usable Microsoft *user* session.

    Xodus is built with --features key-chain-file, so its tokens live in a
    single RON file instead of a D-Bus secret service (which does not exist in
    a Steam Deck Game Mode session or inside a Flatpak sandbox).

    The file existing proves nothing: every command that needs an identity
    provisions device credentials first, which creates the keyring with only a
    'device-tokens' entry. Downloading needs the *user* token that
    `xodus-cli login` stores under 'user-tokens' — without it the download dies
    deep inside Xodus on a missing token instead of asking anyone to sign in.
    """
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
    cannot stand in for. Opens Xodus's own webview window, so it needs
    libwebkit2gtk-4.1 and a display.
    """
    binary = ensure_cli()
    info("Sign in to the Microsoft account that owns Minecraft …")
    proc = subprocess.run([str(binary), "login"], env=_env(),
                          capture_output=True, text=True)
    if proc.returncode != 0 or not signed_in():
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise XodusError(
            "Microsoft sign-in for the Minecraft download did not complete"
            + (f": {detail[-1]}" if detail else ".")
        )
    ok("Microsoft account linked for the Minecraft download.")
    return True


def logout(device=False):
    binary = ensure_cli()
    cmd = [str(binary), "logout"] + (["--device"] if device else [])
    subprocess.run(cmd, env=_env(), capture_output=True, text=True)


def _env():
    env = os.environ.copy()
    # Xodus writes its file keyring under $HOME; keep it explicit so a launcher
    # started with a scrubbed environment still finds the same session.
    env.setdefault("HOME", str(Path.home()))
    return env


# ---------------------------------------------------------------- install


def _bytes(value, unit):
    try:
        return int(float(value) * _UNITS.get(unit, 1))
    except (TypeError, ValueError):
        return 0


def install(product, dest: Path, progress=None):
    """Download + decrypt an edition into ``dest``.

    ``xodus-cli streaming`` is incremental and commits atomically on its own:
    it compares the local segment hashes against the remote package, fetches
    only the changed files, and renames its work package into place at the end.
    So there is deliberately no staging/rollback dance around it here — adding
    one would defeat the delta and re-download the whole 800+ MiB every time.
    """
    binary = ensure_cli()
    if not signed_in():
        raise NotSignedIn(
            "Minecraft is downloaded from Microsoft with your own account, so "
            "you have to sign in before it can be installed.")
    dest.mkdir(parents=True, exist_ok=True)
    cmd = [str(binary), "streaming", product, str(dest)]
    code, tail = _run_streaming(cmd, progress)
    if code != 0:
        text = "\n".join(tail)
        if _NOT_OWNED.search(text):
            raise NotOwned(
                "The linked Microsoft account does not own this edition of "
                "Minecraft. Buy or redeem it on the same account, then try "
                "again.")
        if _NO_CREDENTIALS.search(text):
            raise NotSignedIn(
                "The Microsoft session for the download expired. Sign in "
                "again.")
        raise XodusError(
            f"The Minecraft download failed{': ' + _failure_line(tail) if _failure_line(tail) else '.'}")
    return dest


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


def _run_streaming(cmd, progress=None):
    """Run xodus-cli and translate its progress bars into progress(done, total).

    indicatif hides its bars entirely when stderr is not a terminal, so the
    child gets a pty; without one there would be no progress to report at all.
    """
    tail = []
    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(cmd, stdout=slave, stderr=slave, stdin=slave,
                                env=_env(), close_fds=True)
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


def _sweep_staged_images(older_than=60):
    """Drop staged images a previous launch left behind.

    The loader unlinks its copy within milliseconds of opening it, and the
    wrapper execs and never returns, so anything still named here is from a
    launch that died before the image was mapped. Each one is the size of the
    game executable and /dev/shm is RAM, so they cannot be left to accumulate.
    """
    cutoff = time.time() - older_than
    try:
        leftovers = list(Path("/dev/shm").glob("bol-*"))
    except OSError:
        return
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
So each descriptor is copied into a private file on /dev/shm and the map hands
over that path instead. /dev/shm is RAM, the copy is created 0600, and the
loader unlinks it the instant it opens it, so the decrypted image carries a
name for milliseconds and never reaches durable storage.
"""
import json
import os
import shutil
import sys
import tempfile

ARGV = json.loads({argv!r})
LAUNCHER_PATH = {launcher_path!r}
EXE_NAME = {exe_name!r}

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
    handle, path = tempfile.mkstemp(prefix="bol-", dir="/dev/shm")
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
    sys.exit("could not stage the decrypted game: %s" % exc)

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
os.execvp(ARGV[0], ARGV + [nt_name])
'''


def wrap_encrypted_launch(argv, game_dir: Path, work_dir: Path,
                          launcher_path=None):
    """Turn a launch command into one that can start an encrypted executable.

    The returned command runs ``xodus-cli run`` outermost. It holds the license
    and the XVD decryption, which live in Xodus's Rust crates and are not
    reimplementable here, and it hands the plaintext to Wine as a descriptor
    rather than a file. ``argv``'s last element is the executable path, which
    the wrapper replaces with the NT name Xodus assigns.
    """
    binary = ensure_cli()
    _sweep_staged_images()
    work_dir.mkdir(parents=True, exist_ok=True)
    wrapper = work_dir / "xodus-launch-wrapper.py"
    if launcher_path is None:
        launcher_path = str(Path(__file__).resolve().parent.parent)
    wrapper.write_text(
        _WRAPPER.format(argv=json.dumps(list(argv[:-1])),
                        launcher_path=launcher_path,
                        exe_name=Path(argv[-1]).name),
        encoding="utf-8")
    wrapper.chmod(0o755)
    return [str(binary), "run", str(game_dir), str(wrapper)]

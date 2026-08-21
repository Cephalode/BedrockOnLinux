"""Regression tests for the Xodus acquisition wrapper."""
# SPDX-License-Identifier: MIT

import contextlib
import hashlib
import io
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import xodus


def _cli_archive(path, body=b"#!/bin/sh\nexit 0\n"):
    """A tarball shaped like the published xodus-cli asset."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as archive:
        for name, data in (("xodus-cli", body),
                           ("LICENSE.GPL-3.0", b"GPL"),
                           ("SOURCE-COMMIT", b"deadbeef\n")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755 if name == "xodus-cli" else 0o644
            archive.addfile(info, io.BytesIO(data))
    return hashlib.sha256(path.read_bytes()).hexdigest()


@contextlib.contextmanager
def _own_home(tmp):
    """Point the module at a throwaway Xodus home.

    Every one of these paths is derived from DATA, and the migration writes
    real files, so a test that used the module's own constants would sign the
    developer's machine in or out.
    """
    tmp = Path(tmp)
    home = tmp / "xodus-home"
    with mock.patch.object(xodus, "XODUS_HOME", home), \
            mock.patch.object(xodus, "XODUS_KEYRING",
                              home / ".xodus-keyring.ron"), \
            mock.patch.object(xodus, "LEGACY_XODUS_KEYRING",
                              tmp / "home" / ".xodus-keyring.ron"):
        yield home


class EditionTests(unittest.TestCase):
    def test_known_editions_resolve_to_store_product_ids(self):
        self.assertEqual(xodus.edition("release")["product"], "9NBLGGH2JHXJ")
        self.assertEqual(xodus.edition("preview")["product"], "9P5X4QVLC2XR")
        self.assertIsNone(xodus.edition("nope"))

    def test_list_editions_returns_copies(self):
        first = xodus.list_editions()
        first[0]["name"] = "mutated"
        self.assertNotEqual(xodus.list_editions()[0]["name"], "mutated")


_RELEASE_CID = "7792d9ce-355a-493c-afbd-768f4a77c3b0"
_PREVIEW_CID = "98bd2335-9b01-4e4c-bd05-ccc01614078b"


def _cdn(content_id, build="1.26.4403.0"):
    return (f"http://assets1.xboxlive.com/Z/abc/{content_id}/{build}.def/"
            "Microsoft.MinecraftUWP_x64__8wekyb3d8bbwe.msixvc")


class IndexedUrlTests(unittest.TestCase):
    """The build index decides what gets downloaded, so entries are checked."""

    def setUp(self):
        self.release = xodus.edition("release")

    def test_a_matching_cdn_url_is_accepted(self):
        url = _cdn(_RELEASE_CID)
        self.assertEqual(xodus._indexed_url(self.release, url), url)

    def test_another_host_is_refused(self):
        self.assertIsNone(xodus._indexed_url(
            self.release,
            f"http://evil.example/Z/abc/{_RELEASE_CID}/1.0/x.msixvc"))

    def test_a_lookalike_host_is_refused(self):
        # "xboxlive.com.evil.test" must not pass for "xboxlive.com".
        self.assertIsNone(xodus._indexed_url(
            self.release,
            f"http://assets1.xboxlive.com.evil.test/Z/a/{_RELEASE_CID}/x.msixvc"))

    def test_another_products_content_id_is_refused(self):
        # Otherwise picking Preview could download the Release package.
        self.assertIsNone(
            xodus._indexed_url(self.release, _cdn(_PREVIEW_CID)))

    def test_a_non_package_url_is_refused(self):
        self.assertIsNone(xodus._indexed_url(
            self.release, f"http://assets1.xboxlive.com/Z/a/{_RELEASE_CID}/x.exe"))


class CatalogueTests(unittest.TestCase):
    def _payload(self):
        return {
            "release": {
                "1.26.42.1": [_cdn(_RELEASE_CID, "1.26.4201.0")],
                "1.26.44.3": [_cdn(_RELEASE_CID, "1.26.4403.0")],
                "1.21.120.4": [_cdn(_RELEASE_CID, "1.21.12004.0")],
                "1.26.40.5": ["http://evil.example/x.msixvc"],
            },
            "preview": {"1.26.50.25": [_cdn(_PREVIEW_CID, "1.26.5025.0")]},
        }

    def test_builds_are_listed_newest_first(self):
        with mock.patch.object(xodus, "_fetch_with_fallback",
                               return_value=self._payload()):
            builds = xodus.version_catalogue("release")

        # String order would put 1.21.120.4 above 1.26.42.1.
        self.assertEqual([b["version"] for b in builds],
                         ["1.26.44.3", "1.26.42.1", "1.21.120.4"])

    def test_an_entry_with_no_usable_url_is_dropped(self):
        with mock.patch.object(xodus, "_fetch_with_fallback",
                               return_value=self._payload()):
            builds = xodus.version_catalogue("release")

        self.assertNotIn("1.26.40.5", [b["version"] for b in builds])

    def test_each_edition_reads_its_own_channel(self):
        with mock.patch.object(xodus, "_fetch_with_fallback",
                               return_value=self._payload()):
            builds = xodus.version_catalogue("preview")

        self.assertEqual([b["version"] for b in builds], ["1.26.50.25"])

    def test_a_missing_channel_is_an_error(self):
        with mock.patch.object(xodus, "_fetch_with_fallback",
                               return_value={"release": {}}), \
                self.assertRaises(xodus.XodusError):
            xodus.version_catalogue("preview")

    def test_an_unknown_edition_lists_nothing(self):
        self.assertEqual(xodus.version_catalogue("nope"), [])


class EnsureCliTests(unittest.TestCase):
    def test_matching_digest_installs_the_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            asset = base / "sibling" / "xodus-cli-testrev.tar.gz"
            digest = _cli_archive(asset)
            target = base / "xodus"

            with mock.patch.object(xodus, "XODUS_REV", "testrev"), \
                    mock.patch.object(xodus, "XODUS_ARCHIVE_SHA256", digest), \
                    mock.patch.object(xodus, "XODUS_DIR", target), \
                    mock.patch.object(xodus, "XODUS_BIN", target / "xodus-cli"), \
                    mock.patch.object(xodus.sys, "argv",
                                      [str(asset.parent / "launcher")]):
                binary = xodus.ensure_cli()

            self.assertTrue(binary.is_file())
            self.assertTrue(os.access(binary, os.X_OK))
            self.assertEqual((target / ".rev").read_text().strip(), "testrev")
            # The GPL source offer has to travel with the binary.
            self.assertTrue((target / "LICENSE.GPL-3.0").is_file())

    def test_wrong_digest_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            asset = base / "sibling" / "xodus-cli-testrev.tar.gz"
            _cli_archive(asset)
            target = base / "xodus"

            with mock.patch.object(xodus, "XODUS_REV", "testrev"), \
                    mock.patch.object(xodus, "XODUS_ARCHIVE_SHA256", "00" * 32), \
                    mock.patch.object(xodus, "XODUS_DIR", target), \
                    mock.patch.object(xodus, "XODUS_BIN", target / "xodus-cli"), \
                    mock.patch.object(xodus.sys, "argv",
                                      [str(asset.parent / "launcher")]), \
                    self.assertRaises(xodus.XodusError):
                xodus.ensure_cli()

            self.assertFalse((target / "xodus-cli").exists())

    def test_unset_pin_refuses_to_install_anything(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            asset = base / "sibling" / "xodus-cli-testrev.tar.gz"
            _cli_archive(asset)
            target = base / "xodus"

            with mock.patch.object(xodus, "XODUS_REV", "testrev"), \
                    mock.patch.object(xodus, "XODUS_ARCHIVE_SHA256", ""), \
                    mock.patch.object(xodus, "XODUS_DIR", target), \
                    mock.patch.object(xodus, "XODUS_BIN", target / "xodus-cli"), \
                    mock.patch.object(xodus.sys, "argv",
                                      [str(asset.parent / "launcher")]), \
                    self.assertRaises(xodus.XodusError) as raised:
                xodus.ensure_cli()

            self.assertIn("XODUS_ARCHIVE_SHA256", str(raised.exception))


class SignedInTests(unittest.TestCase):
    @contextlib.contextmanager
    def _keyring(self, tmp, body):
        with _own_home(tmp) as home:
            home.mkdir(parents=True)
            (home / ".xodus-keyring.ron").write_bytes(body)
            yield

    def test_device_credentials_alone_are_not_a_sign_in(self):
        # Every xodus command that needs an identity provisions device
        # credentials first, so the keyring exists long before anyone signs in.
        with tempfile.TemporaryDirectory() as tmp, \
                self._keyring(tmp, b'("device-tokens",("dev_license","..."))'):
            self.assertFalse(xodus.signed_in())

    def test_a_user_token_is_a_sign_in(self):
        with tempfile.TemporaryDirectory() as tmp, \
                self._keyring(tmp, b'("device-tokens",...)("user-tokens",...)'):
            self.assertTrue(xodus.signed_in())

    def test_a_missing_keyring_is_not_a_sign_in(self):
        with tempfile.TemporaryDirectory() as tmp, _own_home(tmp):
            self.assertFalse(xodus.signed_in())


class XodusHomeTests(unittest.TestCase):
    """Where the Microsoft Store session is kept (issue #198).

    Xodus writes its keyring to $HOME, and inside the Flatpak $HOME is a tmpfs
    that goes away with the sandbox. That is not an ordinary sign-out: with no
    device credentials on file the next command provisions a *new* Microsoft
    device, and an account may hold ten before the Store refuses to license
    the game at all.
    """

    def _legacy(self, tmp, body=b'("user-tokens","...")'):
        path = Path(tmp) / "home" / ".xodus-keyring.ron"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
        # What Xodus itself leaves in a home directory: File::create, so
        # 0666 & ~umask -- the mode the Steam Deck report showed.
        os.chmod(path, 0o644)
        return path

    def test_xodus_runs_in_a_home_of_the_launchers_own(self):
        with tempfile.TemporaryDirectory() as tmp, _own_home(tmp) as home, \
                mock.patch.object(xodus.webview, "apply", return_value={}):
            env = xodus._env(Path("/opt/xodus-cli"))

        self.assertEqual(env["HOME"], str(home))

    def test_the_home_is_private(self):
        with tempfile.TemporaryDirectory() as tmp, _own_home(tmp) as home:
            xodus.home()

            # It holds the tokens that are the account.
            self.assertEqual(home.stat().st_mode & 0o777, 0o700)

    def test_a_sign_in_made_before_the_move_is_taken_along(self):
        # Signing in again is not free: it spends one of the ten devices.
        with tempfile.TemporaryDirectory() as tmp, _own_home(tmp) as home:
            legacy = self._legacy(tmp)

            self.assertTrue(xodus.signed_in())

            copied = home / ".xodus-keyring.ron"
            self.assertEqual(copied.read_bytes(), legacy.read_bytes())
            # Tighter than the file it came from: these tokens are the
            # account, and the launcher owns where they land now.
            self.assertEqual(copied.stat().st_mode & 0o777, 0o600)
            # Copied, not moved: an older launcher on the same machine keeps
            # the session it wrote.
            self.assertTrue(legacy.exists())

    def test_the_session_in_use_is_never_overwritten_by_the_old_one(self):
        with tempfile.TemporaryDirectory() as tmp, _own_home(tmp) as home:
            self._legacy(tmp, b'("user-tokens","stale")')
            home.mkdir(parents=True)
            (home / ".xodus-keyring.ron").write_bytes(b'("user-tokens","live")')

            xodus.home()

            self.assertEqual((home / ".xodus-keyring.ron").read_bytes(),
                             b'("user-tokens","live")')

    def test_asking_whether_anyone_is_signed_in_writes_nothing(self):
        # Every window refresh asks; doctor asks. None of them is a reason to
        # create anything in the data directory.
        with tempfile.TemporaryDirectory() as tmp, _own_home(tmp) as home:
            self.assertFalse(xodus.signed_in())

            self.assertFalse(home.exists())

    def test_unlinking_the_account_drops_the_copy_left_behind(self):
        with tempfile.TemporaryDirectory() as tmp, _own_home(tmp), \
                mock.patch.object(xodus, "ensure_cli",
                                  return_value=Path("/bin/true")), \
                mock.patch.object(xodus.webview, "apply", return_value={}):
            legacy = self._legacy(tmp)
            xodus.signed_in()

            xodus.logout()

            # "Unlink this account" cannot leave live tokens in $HOME.
            self.assertFalse(legacy.exists())

    def test_a_keyring_that_cannot_be_copied_is_not_a_sign_in(self):
        # Whatever goes wrong here, the launcher has to keep working: it can
        # offer the sign-in, which is worth one device rather than a crash.
        with tempfile.TemporaryDirectory() as tmp, _own_home(tmp), \
                mock.patch.object(xodus, "_WARNED", {}), \
                mock.patch.object(xodus.tempfile, "mkstemp",
                                  side_effect=OSError("no space")), \
                mock.patch.object(xodus, "warn") as warned:
            self._legacy(tmp)

            self.assertFalse(xodus.signed_in())
            self.assertFalse(xodus.signed_in())

        self.assertEqual(warned.call_count, 1)

    def test_a_home_that_cannot_be_created_is_reported_once(self):
        with tempfile.TemporaryDirectory() as tmp, _own_home(tmp), \
                mock.patch.object(xodus.Path, "mkdir",
                                  side_effect=OSError("read-only")), \
                mock.patch.object(xodus, "_WARNED", {}), \
                mock.patch.object(xodus, "warn") as warned:
            xodus.home()
            xodus.home()

        self.assertEqual(warned.call_count, 1)


class FailureLineTests(unittest.TestCase):
    def test_a_panic_reports_its_message_not_the_backtrace_note(self):
        tail = [
            "thread 'main' (586427) panicked at src/package.rs:86:50:",
            "called `Result::unwrap()` on an `Err` value: NotFound",
            "note: run with `RUST_BACKTRACE=1` environment variable",
        ]
        # Taking the last line would report the note and hide the cause.
        self.assertEqual(xodus._failure_line(tail),
                         "called `Result::unwrap()` on an `Err` value: NotFound")

    def test_an_ordinary_error_reports_its_last_line(self):
        self.assertEqual(
            xodus._failure_line(["connecting", "", "could not reach the CDN"]),
            "could not reach the CDN")

    def test_nothing_printed_reports_nothing(self):
        self.assertEqual(xodus._failure_line([]), "")


class InstallErrorTests(unittest.TestCase):
    def _patched(self, code, tail):
        return (
            mock.patch.object(xodus, "ensure_cli",
                              return_value=Path("/bin/true")),
            mock.patch.object(xodus, "signed_in", return_value=True),
            mock.patch.object(xodus, "_run_streaming",
                              return_value=(code, tail)),
        )

    def test_unowned_game_is_reported_as_ownership(self):
        ensure, signed, stream = self._patched(
            1, ["Package was not found, is it owned by the user?"])
        with tempfile.TemporaryDirectory() as tmp, ensure, signed, stream:
            with self.assertRaises(xodus.NotOwned) as raised:
                xodus.install("9NBLGGH2JHXJ", Path(tmp))
        self.assertIn("does not own", str(raised.exception))

    def test_expired_session_is_reported_as_sign_in(self):
        ensure, signed, stream = self._patched(1, ["Invalid STS token"])
        with tempfile.TemporaryDirectory() as tmp, ensure, signed, stream:
            with self.assertRaises(xodus.NotSignedIn):
                xodus.install("9NBLGGH2JHXJ", Path(tmp))

    def test_an_exhausted_device_pool_says_where_devices_are_removed(self):
        # The account is out of Store devices -- most likely because every
        # Flatpak restart claimed another one (issue #198). Microsoft's own
        # sentence does not say where they are given back.
        ensure, signed, stream = self._patched(1, [
            "not entitled to this content: Device group is full, please "
            "remove a device and try again."])
        with tempfile.TemporaryDirectory() as tmp, ensure, signed, stream:
            with self.assertRaises(xodus.XodusError) as raised:
                xodus.install("9NBLGGH2JHXJ", Path(tmp))
        message = str(raised.exception)
        self.assertIn("account.microsoft.com/devices/content", message)
        # Not an ownership failure, which is what "not entitled" looks like.
        self.assertNotIsInstance(raised.exception, xodus.NotOwned)

    def test_other_failures_keep_the_last_line(self):
        ensure, signed, stream = self._patched(1, ["", "disk on fire"])
        with tempfile.TemporaryDirectory() as tmp, ensure, signed, stream:
            with self.assertRaises(xodus.XodusError) as raised:
                xodus.install("9NBLGGH2JHXJ", Path(tmp))
        self.assertIn("disk on fire", str(raised.exception))

    def test_signed_out_never_starts_a_download(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(xodus, "ensure_cli",
                                  return_value=Path("/bin/true")), \
                mock.patch.object(xodus, "signed_in", return_value=False), \
                mock.patch.object(xodus, "_run_streaming") as stream:
            with self.assertRaises(xodus.NotSignedIn):
                xodus.install("9NBLGGH2JHXJ", Path(tmp))
        stream.assert_not_called()


def _install_build(dest):
    """Write what a finished xodus-cli download leaves behind."""
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "Minecraft.Windows.exe").write_bytes(b"MZ")
    (dest / "appxmanifest.xml").write_text("<Package/>")
    (dest / ".xodus-streaming.msixvc").write_bytes(b"cache")


class InstallCacheTests(unittest.TestCase):
    """A retry has to be a retry.

    xodus-cli re-opens the package cache it left in the destination, so a
    short one poisons every later attempt: it panics reading past the end
    ("cache ended before cached_len"), or exits 0 with an empty delta and
    installs nothing at all.
    """

    def _install(self, dest, runs, product="9NBLGGH2JHXJ"):
        calls = []

        def fake(cmd, progress=None):
            calls.append(cmd[2])
            return runs[len(calls) - 1](dest)

        with mock.patch.object(xodus, "ensure_cli",
                               return_value=Path("/bin/true")), \
                mock.patch.object(xodus, "signed_in", return_value=True), \
                mock.patch.object(xodus, "_run_streaming", side_effect=fake):
            try:
                xodus.install(product, dest)
            except xodus.XodusError as exc:
                return calls, exc
        return calls, None

    def test_a_failed_download_drops_the_cache_it_left_behind(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "1.26.42.1"
            dest.mkdir()
            (dest / ".xodus-streaming.msixvc").write_bytes(b"truncated")
            (dest / ".xodus-streaming-tmp.msixvc").write_bytes(b"partial")
            _, exc = self._install(dest, [lambda d: (101, ["panicked at x:1:2", "ok: Header(Io(...))"])])
            self.assertIsNotNone(exc)
            self.assertEqual(list(dest.glob(".xodus-streaming*")), [])

    def test_a_failed_reinstall_keeps_a_playable_builds_cache(self):
        # xodus-cli run decrypts the keep_encrypted segments out of that file
        # at every launch, so deleting it would break the build still on disk.
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "1.26.44.3"
            _install_build(dest)
            (dest / ".xodus-streaming-tmp.msixvc").write_bytes(b"partial")
            self._install(dest, [lambda d: (1, ["the CDN hung up"])])
            self.assertTrue((dest / ".xodus-streaming.msixvc").exists())
            self.assertFalse((dest / ".xodus-streaming-tmp.msixvc").exists())

    def test_exiting_zero_without_installing_anything_is_a_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "1.26.42.1"
            dest.mkdir()
            (dest / ".xodus-streaming.msixvc").write_bytes(b"truncated")
            _, exc = self._install(dest, [lambda d: (0, ["Complete"])])
            self.assertIn("installed no game", str(exc))
            self.assertEqual(list(dest.glob(".xodus-streaming*")), [])

    def test_exiting_zero_without_the_package_is_a_failure(self):
        # Every path that ends xodus-cli early -- no licence, no disk space --
        # returns before it renames the package into place, and still exits 0.
        # Over an older build already unpacked here that read as a finished
        # install, and the game died at launch instead, on a package that was
        # never written.
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "1.26.44.3"
            _install_build(dest)
            (dest / xodus.PACKAGE_CACHE).unlink()
            _, exc = self._install(
                dest,
                [lambda d: (0, ["not enough free disk space on /home: need "
                                "8 bytes, have 4 bytes (files: 8)"])])
            self.assertIsNotNone(exc)
            self.assertIn("free disk space", str(exc))

    def test_a_truncated_mirror_falls_through_to_the_next_one(self):
        mirrors = ["http://assets1.xboxlive.com/a.msixvc",
                   "http://assets2.xboxlive.com/a.msixvc"]
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "1.26.42.1"

            def truncated(d):
                (d / ".xodus-streaming.msixvc").write_bytes(b"short")
                return (101, ["panicked at x:1:2", "ok: Header(Io(...))"])

            def complete(d):
                _install_build(d)
                return (0, ["Complete"])

            calls, exc = self._install(dest, [truncated, complete], mirrors)
            self.assertIsNone(exc)
            self.assertEqual(calls, mirrors)
            # The good mirror's cache survives; only the bad one's was dropped.
            self.assertTrue((dest / ".xodus-streaming.msixvc").exists())

    def test_an_ownership_failure_does_not_try_the_next_mirror(self):
        mirrors = ["http://assets1.xboxlive.com/a.msixvc",
                   "http://assets2.xboxlive.com/a.msixvc"]
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "1.26.42.1"
            calls, exc = self._install(
                dest,
                [lambda d: (1, ["Package was not found, is it owned by the "
                                "user?"])],
                mirrors)
        self.assertIsInstance(exc, xodus.NotOwned)
        self.assertEqual(calls, mirrors[:1])


class GameRootTests(unittest.TestCase):
    def test_a_complete_build_is_found(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / "1.26.44.3"
            _install_build(dest / "games")
            self.assertEqual(xodus.game_root(dest), dest / "games")

    def test_an_exe_without_a_manifest_is_a_truncated_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp)
            (dest / "Minecraft.Windows.exe").write_bytes(b"MZ")
            self.assertIsNone(xodus.game_root(dest))

    def test_a_missing_directory_is_not_a_build(self):
        self.assertIsNone(xodus.game_root(Path("/nonexistent/build")))


class ProgressTests(unittest.TestCase):
    def test_only_the_total_bar_drives_progress(self):
        seen = []
        tail = []
        # Per-file bars would make the launcher's progress jump backwards.
        xodus._consume(
            "...raries\\Minecraft.Windows.exe    1.00 MiB/    2.00 MiB",
            tail, seen.append)
        self.assertEqual(seen, [])

        captured = []
        xodus._consume(
            "Downloading    431.00 MiB/    862.00 MiB     12.00 MiB [###] 50%",
            tail, lambda done, total: captured.append((done, total)))
        self.assertEqual(captured, [(431 << 20, 862 << 20)])

    def test_a_session_without_a_terminal_still_gets_the_bars_drawn(self):
        # A launcher started from a desktop entry, Steam or Game Mode passes
        # no TERM, and indicatif draws nothing when TERM says the terminal
        # cannot be drawn on -- the download then arrives completely silent
        # and the launcher can only sweep a busy bar for gigabytes.
        for value in (None, "", "dumb", "unknown"):
            env = {} if value is None else {"TERM": value}
            self.assertEqual(xodus._drawable_term(env)["TERM"],
                             "xterm-256color")

    def test_a_terminal_the_session_named_is_left_alone(self):
        self.assertEqual(
            xodus._drawable_term({"TERM": "screen-256color"})["TERM"],
            "screen-256color")

    def test_the_first_bar_of_a_download_reports_real_progress(self):
        # Exactly what xodus-cli prints once it is drawing (captured from a
        # live download), padding and all: the launcher has to read progress
        # out of its opening "Initializing" phase, not only out of the later
        # "Downloading" one, or the bar sits at nothing for the whole package.
        captured = []
        xodus._consume(
            "Initializing                      12.44 MiB/    2.32 GiB  "
            "20.83 MiB/s [>---------------------------------------]   1%",
            [], lambda done, total: captured.append((done, total)))
        self.assertEqual(captured, [(int(12.44 * (1 << 20)), int(2.32 * (1 << 30)))])

    def test_non_progress_output_is_kept_for_the_error_message(self):
        tail = []
        xodus._consume("could not reach the CDN", tail, None)
        self.assertEqual(tail, ["could not reach the CDN"])

    def test_the_kept_output_stays_bounded(self):
        tail = []
        for i in range(200):
            xodus._consume(f"line {i}", tail, None)
        self.assertEqual(len(tail), 40)
        self.assertEqual(tail[-1], "line 199")


class EncryptedExeTests(unittest.TestCase):
    def test_plaintext_pe_is_not_encrypted(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "Minecraft.Windows.exe"
            exe.write_bytes(b"MZ\x90\x00")
            self.assertFalse(xodus.exe_is_encrypted(exe))

    def test_ciphertext_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "Minecraft.Windows.exe"
            exe.write_bytes(b"\x17\xc3\x00\x91")
            self.assertTrue(xodus.exe_is_encrypted(exe))

    def test_a_missing_file_is_not_reported_as_encrypted(self):
        # A missing exe is a broken install, handled elsewhere; claiming it is
        # encrypted would send the launcher down the memfd path for nothing.
        self.assertFalse(xodus.exe_is_encrypted(Path("/nonexistent/x.exe")))


def _pe32plus_header(stack_reserve=0x100000):
    """The smallest PE32+ header carrying a SizeOfStackReserve field."""
    head = bytearray(0x400)
    head[0:2] = b"MZ"
    struct.pack_into("<I", head, 0x3C, 0x80)          # e_lfanew
    head[0x80:0x84] = b"PE\0\0"
    opt = 0x80 + 4 + 20
    struct.pack_into("<H", head, opt, 0x20B)          # PE32+ magic
    struct.pack_into("<Q", head, opt + 72, stack_reserve)
    return bytes(head)


def _stack_reserve(data):
    opt = struct.unpack_from("<I", data, 0x3C)[0] + 4 + 20
    return struct.unpack_from("<Q", data, opt + 72)[0]


class StagingDirTests(unittest.TestCase):
    FLATPAK = {"FLATPAK_ID": "io.github.wyze3306.BedrockOnLinux",
               "XDG_RUNTIME_DIR": "/run/user/1000"}

    def test_a_plain_install_stages_on_dev_shm(self):
        self.assertEqual(xodus.staging_dir(environ={},
                                           info_path="/nonexistent"),
                         Path("/dev/shm"))

    def test_flatpak_stages_where_the_container_can_look(self):
        # pressure-vessel builds the container as a new Flatpak app instance,
        # which gets its own /dev/shm ("not shared between app instances",
        # flatpak#4214): the staged image was invisible to Wine and the game
        # died on "ShellExecuteEx failed: File not found" (issue #193). The
        # per-application $XDG_RUNTIME_DIR is bound into every instance.
        self.assertEqual(
            xodus.staging_dir(environ=self.FLATPAK, info_path="/nonexistent"),
            Path("/run/user/1000/bedrock-on-linux"))

    def test_flatpak_is_recognised_without_the_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            info = Path(tmp) / ".flatpak-info"
            info.write_text("[Application]\n", encoding="utf-8")
            self.assertEqual(
                xodus.staging_dir(environ={"XDG_RUNTIME_DIR": "/run/user/9"},
                                  info_path=info),
                Path("/run/user/9/bedrock-on-linux"))

    def test_a_flatpak_without_a_runtime_dir_keeps_the_old_location(self):
        self.assertEqual(
            xodus.staging_dir(environ={"FLATPAK_ID": "io.github.x"},
                              info_path="/nonexistent"),
            Path("/dev/shm"))


class WrapEncryptedLaunchTests(unittest.TestCase):
    EXE = "/games/release/1.26.44.3/Minecraft.Windows.exe"
    NT = "\\??\\Z:\\games\\release\\1.26.44.3\\Minecraft.Windows.exe"

    def _wrap(self, tmp, argv, stage_dir=None, env=None):
        # xodus-cli decrypts the executable out of the package it keeps beside
        # it, so an encrypted build that can start always has one.
        game = Path(tmp) / "game"
        game.mkdir(parents=True, exist_ok=True)
        (game / xodus.PACKAGE_CACHE).write_bytes(b"package")
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(
                xodus, "ensure_cli", return_value=Path("/opt/xodus-cli")))
            # An encrypted build is licensed at every launch, so the wrapper
            # is only ever built for an account that is linked.
            stack.enter_context(mock.patch.object(
                xodus, "signed_in", return_value=True))
            stack.enter_context(mock.patch.object(
                xodus.webview, "apply", return_value={}))
            if stage_dir is not None:
                stack.enter_context(mock.patch.object(
                    xodus, "staging_dir", return_value=Path(stage_dir)))
            return xodus.wrap_encrypted_launch(argv, Path(tmp) / "game",
                                               Path(tmp) / "run", env=env)

    def test_running_an_encrypted_build_needs_the_store_account(self):
        # It used to get as far as xodus-cli, which died on a missing keyring
        # entry -- a Rust panic, in a launcher that has a button for exactly
        # this (issue #198).
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(xodus, "ensure_cli",
                                  return_value=Path("/opt/xodus-cli")), \
                mock.patch.object(xodus, "signed_in", return_value=False):
            with self.assertRaises(xodus.NotSignedIn):
                xodus.wrap_encrypted_launch(
                    [sys.executable, self.EXE], Path(tmp) / "game",
                    Path(tmp) / "run")

    def test_a_build_without_its_package_is_named_not_panicked_on(self):
        # xodus-cli opens the package unconditionally and unwraps the error,
        # so a game directory that lost it took the launcher down with a Rust
        # panic ("run.rs:133 ... Os { code: 2, kind: NotFound }").
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(xodus, "ensure_cli",
                                  return_value=Path("/opt/xodus-cli")), \
                mock.patch.object(xodus, "signed_in", return_value=True):
            game = Path(tmp) / "game"
            game.mkdir()
            with self.assertRaises(xodus.XodusError) as raised:
                xodus.wrap_encrypted_launch(
                    [sys.executable, self.EXE], game, Path(tmp) / "run")
        message = str(raised.exception)
        self.assertIn(xodus.PACKAGE_CACHE, message)
        self.assertIn(str(game), message)

    def test_xodus_reads_the_licence_from_the_launchers_home(self):
        with tempfile.TemporaryDirectory() as tmp, _own_home(tmp) as home:
            env = {"HOME": "/home/player"}

            self._wrap(tmp, [sys.executable, self.EXE], env=env)

            self.assertEqual(env["HOME"], str(home))

    def test_the_game_is_handed_back_the_players_own_home(self):
        # Wine, umu and the Steam runtime all keep state under $HOME; only
        # xodus-cli, one exec further out, wants the launcher's.
        with tempfile.TemporaryDirectory() as tmp, _own_home(tmp):
            recorder, argv = self._recorder(tmp, "os.environ['HOME']")
            cmd = self._wrap(tmp, argv, env={"HOME": "/home/player"})
            fd = self._memfd()

            self._run(cmd[3], self.NT, f"{fd}:{self.NT}", (fd,), check=True)

            self.assertEqual(recorder.read_text(), "/home/player")

    def test_images_left_by_a_dead_launch_are_swept(self):
        stale = Path(tempfile.mkstemp(prefix="bol-", dir="/dev/shm")[1])
        os.close(os.open(stale, os.O_RDONLY))
        os.utime(stale, (0, 0))
        fresh = Path(tempfile.mkstemp(prefix="bol-", dir="/dev/shm")[1])
        self.addCleanup(lambda: fresh.unlink(missing_ok=True))
        self.addCleanup(lambda: stale.unlink(missing_ok=True))

        with tempfile.TemporaryDirectory() as tmp:
            self._wrap(tmp, [sys.executable, "-c", "pass", self.EXE])

        # The loader unlinks its own copy in milliseconds, so anything still
        # named is from a launch that died -- and each one is the size of the
        # game executable, in RAM.
        self.assertFalse(stale.exists())
        # A copy a concurrent launch just staged must survive.
        self.assertTrue(fresh.exists())

    def test_both_staging_locations_are_swept(self):
        # An install that switches layout (Flatpak or not) must not leave the
        # other location's leftovers sitting in RAM forever.
        with tempfile.TemporaryDirectory() as tmp:
            runtime = Path(tmp) / "run-user"
            runtime.mkdir()
            stale = Path(tempfile.mkstemp(prefix="bol-", dir=runtime)[1])
            os.utime(stale, (0, 0))
            shm = Path(tempfile.mkstemp(prefix="bol-", dir="/dev/shm")[1])
            self.addCleanup(lambda: shm.unlink(missing_ok=True))
            os.utime(shm, (0, 0))

            self._wrap(tmp, [sys.executable, "-c", "pass", self.EXE],
                       stage_dir=runtime)

            self.assertFalse(stale.exists())
            self.assertFalse(shm.exists())

    def test_the_staging_directory_is_created_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "run-user" / "bedrock-on-linux"

            self._wrap(tmp, [sys.executable, "-c", "pass", self.EXE],
                       stage_dir=stage)

            # $XDG_RUNTIME_DIR/<app> does not exist until something makes it,
            # and the decrypted image must be no more readable there than the
            # 0600 copies it holds.
            self.assertTrue(stage.is_dir())
            self.assertEqual(stage.stat().st_mode & 0o777, 0o700)

    def test_an_existing_staging_directory_is_left_alone(self):
        # The default one is /dev/shm, which belongs to the system: the
        # launcher creates its staging directory, it never re-permissions one.
        before = Path("/dev/shm").stat().st_mode
        with tempfile.TemporaryDirectory() as tmp:
            existing = Path(tmp) / "already-there"
            existing.mkdir(mode=0o755)

            self._wrap(tmp, [sys.executable, "-c", "pass", self.EXE])
            self._wrap(tmp, [sys.executable, "-c", "pass", self.EXE],
                       stage_dir=existing)

            self.assertEqual(existing.stat().st_mode & 0o777, 0o755)
        self.assertEqual(Path("/dev/shm").stat().st_mode, before)

    def test_command_runs_xodus_over_the_game_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = self._wrap(tmp, ["/usr/bin/python3", "/opt/umu-run", self.EXE])

        self.assertEqual(cmd[:2], ["/opt/xodus-cli", "run"])
        self.assertEqual(cmd[2], str(Path(tmp) / "game"))
        self.assertEqual(cmd[3], str(Path(tmp) / "run"
                                     / "xodus-launch-wrapper.py"))
        # Naming the executable to xodus-cli would mean guessing how the
        # package spells its own segment keys, and a wrong guess is fatal
        # there; the wrapper picks it out of the map by name instead.
        self.assertNotIn("--exe", cmd)

    def _memfd(self, payload=None):
        fd = os.memfd_create("bol-test", 0)
        os.write(fd, payload if payload is not None else _pe32plus_header())
        os.set_inheritable(fd, True)
        self.addCleanup(lambda: os.close(fd))
        return fd

    def _run(self, wrapper, argv1, file_map, fds, **kwargs):
        return subprocess.run(
            [sys.executable, wrapper, argv1], pass_fds=tuple(fds),
            env={**os.environ, "WINE_DLL_FILE_MAP": file_map}, **kwargs)

    def _recorder(self, tmp, script):
        path = Path(tmp) / "out.txt"
        return path, [sys.executable, "-c",
                      f"import os, sys, pathlib; "
                      f"pathlib.Path({str(path)!r}).write_text({script})",
                      self.EXE]

    def test_the_executable_is_chosen_by_name_not_by_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder, argv = self._recorder(tmp, "sys.argv[-1]")
            cmd = self._wrap(tmp, argv)
            exe_fd, other_fd = self._memfd(), self._memfd()
            other = "\\??\\Z:\\games\\release\\1.26.44.3\\other.dll"

            # The executable is deliberately not the first entry, and the
            # argument xodus passed names the wrong file.
            self._run(cmd[3], other,
                      f"{other_fd}:{other}|{exe_fd}:{self.NT}",
                      (exe_fd, other_fd), check=True)

            self.assertEqual(recorder.read_text(), self.NT)

    def test_the_map_hands_over_paths_not_descriptors(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder, argv = self._recorder(
                tmp, 'os.environ["WINE_DLL_FILE_MAP"]')
            cmd = self._wrap(tmp, argv)
            fd = self._memfd()

            self._run(cmd[3], self.NT, f"{fd}:{self.NT}", (fd,), check=True)
            converted = recorder.read_text()

        # A descriptor number means nothing inside the Steam Linux Runtime
        # container, which is why Wine died on "Bad file descriptor".
        path, _, mapped = converted.partition(":")
        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))
        self.assertEqual(mapped, self.NT)
        self.assertTrue(path.startswith("/dev/shm/bol-"), path)
        self.assertTrue(Path(path).is_file())
        # RAM-backed and private: the decrypted image is readable by nobody
        # else while it briefly has a name.
        self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)

    def test_the_image_is_staged_where_the_launcher_asked(self):
        # Under Flatpak that is $XDG_RUNTIME_DIR, the only RAM the nested
        # container shares with the launcher (issue #193).
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "run-user" / "bedrock-on-linux"
            recorder, argv = self._recorder(
                tmp, 'os.environ["WINE_DLL_FILE_MAP"]')
            cmd = self._wrap(tmp, argv, stage_dir=stage)
            fd = self._memfd()

            self._run(cmd[3], self.NT, f"{fd}:{self.NT}", (fd,), check=True)
            path, _, mapped = recorder.read_text().partition(":")

            self.assertEqual(mapped, self.NT)
            self.assertEqual(Path(path).parent, stage)
            self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)

    def test_a_staging_directory_that_vanished_is_recreated(self):
        # $XDG_RUNTIME_DIR is cleaned out from under long-lived processes;
        # the wrapper runs one exec before the game and cannot bail there.
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp) / "run-user" / "bedrock-on-linux"
            recorder, argv = self._recorder(
                tmp, 'os.environ["WINE_DLL_FILE_MAP"]')
            cmd = self._wrap(tmp, argv, stage_dir=stage)
            shutil.rmtree(stage)
            fd = self._memfd()

            self._run(cmd[3], self.NT, f"{fd}:{self.NT}", (fd,), check=True)
            staged = Path(recorder.read_text().partition(":")[0])

            self.assertEqual(staged.parent, stage)

    def test_the_staged_image_carries_the_raised_stack_reserve(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder, argv = self._recorder(
                tmp, 'os.environ["WINE_DLL_FILE_MAP"]')
            cmd = self._wrap(tmp, argv)
            fd = self._memfd()

            self._run(cmd[3], self.NT, f"{fd}:{self.NT}", (fd,), check=True)
            path = recorder.read_text().partition(":")[0]

        self.addCleanup(lambda: Path(path).unlink(missing_ok=True))
        # A Store package has no on-disk header to edit, so the staged copy is
        # the only place the settings/pause fix (#27) can land.
        self.assertEqual(_stack_reserve(Path(path).read_bytes()), 0x1000000)
        # The descriptor xodus handed over is left as it was.
        self.assertEqual(_stack_reserve(os.pread(fd, 0x400, 0)), 0x100000)

    def test_a_broken_map_entry_still_launches_the_game(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder, argv = self._recorder(tmp, "'ran'")
            cmd = self._wrap(tmp, argv)

            # Nothing usable to stage: the game must still start, because one
            # that starts without the fix beats one that does not start.
            self._run(cmd[3], self.NT, f"notanfd:{self.NT}", (), check=True)

            self.assertEqual(recorder.read_text(), "ran")

    def test_nothing_to_launch_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = self._wrap(tmp, [sys.executable, "-c", "pass", self.EXE])
            result = subprocess.run([sys.executable, cmd[3]],
                                    capture_output=True, text=True,
                                    env={**os.environ,
                                         "WINE_DLL_FILE_MAP": ""})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("NT executable name", result.stderr)


if __name__ == "__main__":
    unittest.main()

"""Regression tests for the Xodus acquisition wrapper."""
# SPDX-License-Identifier: MIT

import hashlib
import io
import os
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
    def _keyring(self, tmp, body):
        path = Path(tmp) / "keyring.ron"
        path.write_bytes(body)
        return mock.patch.object(xodus, "XODUS_KEYRING", path)

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
        with mock.patch.object(xodus, "XODUS_KEYRING",
                               Path("/nonexistent/keyring.ron")):
            self.assertFalse(xodus.signed_in())


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


class WrapEncryptedLaunchTests(unittest.TestCase):
    EXE = "/games/release/1.26.44.3/Minecraft.Windows.exe"
    NT = "\\??\\Z:\\games\\release\\1.26.44.3\\Minecraft.Windows.exe"

    def _wrap(self, tmp, argv):
        with mock.patch.object(xodus, "ensure_cli",
                               return_value=Path("/opt/xodus-cli")):
            return xodus.wrap_encrypted_launch(argv, Path(tmp) / "game",
                                               Path(tmp) / "run")

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

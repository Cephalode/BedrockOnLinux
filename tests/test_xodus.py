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
    def _wrap(self, tmp, argv):
        with mock.patch.object(xodus, "ensure_cli",
                               return_value=Path("/opt/xodus-cli")):
            return xodus.wrap_encrypted_launch(argv, Path(tmp) / "game",
                                               Path(tmp) / "run")

    def test_command_runs_xodus_and_names_the_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = self._wrap(
                tmp, ["/usr/bin/python3", "/opt/umu-run",
                      "/games/release/Minecraft.Windows.exe"])

        self.assertEqual(cmd[:2], ["/opt/xodus-cli", "run"])
        self.assertEqual(cmd[3], str(Path(tmp) / "run"
                                     / "xodus-launch-wrapper.py"))
        # Naming the executable matters: xodus otherwise takes whichever
        # keep_encrypted entry its hash map yields first.
        self.assertEqual(cmd[-2:], ["--exe", "\\Minecraft.Windows.exe"])

    def test_wrapper_appends_the_nt_name_to_the_launch_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = Path(tmp) / "argv.txt"
            cmd = self._wrap(tmp, [
                sys.executable, "-c",
                "import sys, pathlib; "
                f"pathlib.Path({str(recorder)!r}).write_text('\\n'.join(sys.argv[1:]))",
                "/games/release/Minecraft.Windows.exe"])
            wrapper = cmd[3]

            nt_name = "\\??\\Z:\\games\\release\\Minecraft.Windows.exe"
            subprocess.run([sys.executable, wrapper, nt_name], check=True,
                           env={**os.environ, "WINE_DLL_FILE_MAP": ""})

            # The launcher's own command survives; only the executable
            # argument is replaced by the name xodus assigned.
            self.assertEqual(recorder.read_text().splitlines()[-1], nt_name)

    def test_wrapper_raises_the_stack_reserve_of_the_mapped_descriptor(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = self._wrap(tmp, [sys.executable, "-c", "pass",
                                   "/games/release/Minecraft.Windows.exe"])
            wrapper = cmd[3]

            fd = os.memfd_create("test-exe", 0)
            try:
                os.write(fd, _pe32plus_header())
                os.set_inheritable(fd, True)
                nt_name = "\\??\\Z:\\games\\Minecraft.Windows.exe"
                subprocess.run(
                    [sys.executable, wrapper, nt_name], check=True,
                    pass_fds=(fd,),
                    env={**os.environ,
                         "WINE_DLL_FILE_MAP": f"{fd}:{nt_name}"})
                patched = os.pread(fd, 0x400, 0)
            finally:
                os.close(fd)

        # The loader reads SizeOfStackReserve when it maps the image, and a
        # Store package has no on-disk header to edit, so the memfd is the only
        # place the settings/pause fix (#27) can land.
        self.assertEqual(_stack_reserve(patched), 0x1000000)

    def test_wrapper_leaves_other_descriptors_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = self._wrap(tmp, [sys.executable, "-c", "pass",
                                   "/games/release/Minecraft.Windows.exe"])
            wrapper = cmd[3]

            fd = os.memfd_create("other-exe", 0)
            try:
                os.write(fd, _pe32plus_header())
                os.set_inheritable(fd, True)
                subprocess.run(
                    [sys.executable, wrapper, "\\??\\Z:\\wanted.exe"],
                    check=True, pass_fds=(fd,),
                    env={**os.environ,
                         "WINE_DLL_FILE_MAP": f"{fd}:\\??\\Z:\\other.exe"})
                untouched = os.pread(fd, 0x400, 0)
            finally:
                os.close(fd)

        self.assertEqual(_stack_reserve(untouched), 0x100000)

    def test_wrapper_still_launches_when_the_header_cannot_be_patched(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = Path(tmp) / "ran.txt"
            cmd = self._wrap(tmp, [
                sys.executable, "-c",
                f"import pathlib; pathlib.Path({str(recorder)!r}).write_text('ran')",
                "/games/release/Minecraft.Windows.exe"])
            wrapper = cmd[3]

            # A game that starts with a 1 MB stack beats one that does not
            # start at all, so a bad map entry must never be fatal.
            subprocess.run(
                [sys.executable, wrapper, "\\??\\Z:\\x.exe"], check=True,
                env={**os.environ, "WINE_DLL_FILE_MAP": "notanfd:\\??\\Z:\\x.exe"})

            self.assertEqual(recorder.read_text(), "ran")

    def test_wrapper_refuses_to_run_without_an_executable_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            cmd = self._wrap(tmp, [sys.executable, "-c", "pass", "/x.exe"])
            result = subprocess.run([sys.executable, cmd[3]],
                                    capture_output=True, text=True)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("NT executable name", result.stderr)


if __name__ == "__main__":
    unittest.main()

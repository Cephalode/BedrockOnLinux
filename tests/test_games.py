"""Regression tests for Minecraft edition installation through Xodus."""
# SPDX-License-Identifier: MIT

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from bol import games
from bol.log import BolError


def _write_game(root, marker=b"MZ game"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "Minecraft.Windows.exe").write_bytes(marker)
    (root / "AppxManifest.xml").write_text(
        '<Package><Identity Version="1.26.3301.0" /></Package>',
        encoding="utf-8")
    return root


class EditionListingTests(unittest.TestCase):
    def test_beta_edition_is_hidden_unless_requested(self):
        with mock.patch.object(games, "GAMES", Path("/nonexistent")):
            stable = games.list_editions(include_beta=False)
            everything = games.list_editions(include_beta=True)

        self.assertTrue(stable)
        self.assertTrue(all(not e["beta"] for e in stable))
        self.assertTrue(any(e["beta"] for e in everything))
        self.assertGreater(len(everything), len(stable))

    def test_listing_reports_the_installed_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_game(base / "release")
            with mock.patch.object(games, "GAMES", base):
                editions = games.list_editions(include_beta=True)

        by_id = {e["id"]: e for e in editions}
        # The store serves only the current build, so the number shown comes
        # from the manifest of what is actually installed.
        self.assertEqual(by_id["release"]["installed"], "1.26.33.1")
        self.assertIsNone(by_id["preview"]["installed"])


class InstallTests(unittest.TestCase):
    def _edition(self):
        return {"id": "release", "product": "9NBLGGH2JHXJ",
                "name": "Minecraft for Windows", "beta": False}

    def test_fresh_install_streams_and_records_the_product(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            edition = self._edition()

            def fake_install(product, dest, progress=None):
                _write_game(Path(dest))
                return dest

            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(games.xodus, "install",
                                      side_effect=fake_install) as install:
                root = games.install_game(edition)

            install.assert_called_once()
            self.assertEqual(install.call_args[0][0], "9NBLGGH2JHXJ")
            self.assertTrue((root / "Minecraft.Windows.exe").exists())
            record = json.loads(
                (base / "release" / games._INSTALL_METADATA).read_text())
            self.assertEqual(record["product"], "9NBLGGH2JHXJ")

    def test_recent_install_is_not_restreamed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_game(base / "release")
            games._write_install_record(base / "release", self._edition())

            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(games.xodus, "install") as install:
                games.install_game(self._edition())

            # Re-checking the CDN on every launch would cost package metadata
            # for nothing; the delta check is on an interval.
            install.assert_not_called()

    def test_stale_install_is_delta_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_game(base / "release")
            games._write_install_record(base / "release", self._edition())
            record_path = base / "release" / games._INSTALL_METADATA
            record = json.loads(record_path.read_text())
            record["checked"] = int(time.time()) - games._UPDATE_INTERVAL - 1
            record_path.write_text(json.dumps(record), encoding="utf-8")

            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(games.xodus, "install") as install:
                games.install_game(self._edition())

            install.assert_called_once()

    def test_clock_moving_backwards_does_not_freeze_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_game(base / "release")
            games._write_install_record(base / "release", self._edition())
            record_path = base / "release" / games._INSTALL_METADATA
            record = json.loads(record_path.read_text())
            record["checked"] = int(time.time()) + 10 * games._UPDATE_INTERVAL
            record_path.write_text(json.dumps(record), encoding="utf-8")

            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(games.xodus, "install") as install:
                games.install_game(self._edition())

            install.assert_called_once()

    def test_a_different_product_in_the_folder_is_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_game(base / "release")
            games._write_install_record(
                base / "release",
                {"id": "release", "product": "SOMETHINGELSE"})

            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(games.xodus, "install") as install:
                games.install_game(self._edition())

            install.assert_called_once()

    def test_incomplete_tree_after_streaming_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            def truncated(product, dest, progress=None):
                Path(dest).mkdir(parents=True, exist_ok=True)
                (Path(dest) / "Minecraft.Windows.exe").write_bytes(b"MZ")
                return dest

            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(games.xodus, "install",
                                      side_effect=truncated), \
                    self.assertRaises(BolError):
                games.install_game(self._edition())


class SelectionTests(unittest.TestCase):
    def test_managed_folder_records_edition_and_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            games_dir = base / "games"
            root = _write_game(games_dir / "preview")
            settings = {}

            with mock.patch.object(games, "GAMES", games_dir), \
                    mock.patch.object(games, "CONTENT", base / "content"), \
                    mock.patch.object(games, "load_settings",
                                      side_effect=lambda: dict(settings)), \
                    mock.patch.object(games, "save_settings",
                                      side_effect=settings.update):
                games.use_game_dir(root)

        self.assertEqual(settings["mc_edition"], "preview")
        self.assertEqual(settings["mc_version"], "1.26.33.1")

    def test_imported_folder_leaves_the_edition_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            root = _write_game(base / "elsewhere")
            settings = {"mc_edition": "release"}

            with mock.patch.object(games, "GAMES", base / "games"), \
                    mock.patch.object(games, "CONTENT", base / "content"), \
                    mock.patch.object(games, "load_settings",
                                      side_effect=lambda: dict(settings)), \
                    mock.patch.object(games, "save_settings",
                                      side_effect=settings.update):
                games.use_game_dir(root)

        # A copy from outside the managed tree is not an edition; keeping the
        # previous choice would let the next setup reinstall over it.
        self.assertEqual(settings["mc_edition"], "release")
        self.assertEqual(settings["game_dir"], str(root.resolve()))

    def test_auto_edition_prefers_the_remembered_choice(self):
        chosen = games._auto_edition({"mc_edition": "preview"})
        self.assertEqual(chosen["id"], "preview")

    def test_auto_edition_falls_back_to_the_stable_one(self):
        with mock.patch.object(games, "GAMES", Path("/nonexistent")):
            chosen = games._auto_edition({})
        self.assertFalse(chosen["beta"])


class ManifestVersionTests(unittest.TestCase):
    def test_appx_third_field_is_split_back_into_minor_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "AppxManifest.xml").write_text(
                '<Package><Identity Version="1.26.2004.0" /></Package>',
                encoding="utf-8")
            self.assertEqual(games.mc_version_str(root), "1.26.20.4")


if __name__ == "__main__":
    unittest.main()

"""Regression tests for Minecraft edition installation through Xodus."""
# SPDX-License-Identifier: MIT

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bol import games
from bol.log import BolError


_CATALOGUE = (
    {"version": "1.26.44.3",
     "urls": ["http://assets1.xboxlive.com/Z/a/"
              "7792d9ce-355a-493c-afbd-768f4a77c3b0/1.26.4403.0.b/x.msixvc"]},
    {"version": "1.26.42.1",
     "urls": ["http://assets1.xboxlive.com/Z/a/"
              "7792d9ce-355a-493c-afbd-768f4a77c3b0/1.26.4201.0.b/x.msixvc"]},
)


def _catalogue(installed=()):
    return [dict(entry, installed=entry["version"] in installed)
            for entry in _CATALOGUE]


def _write_game(root, marker=b"MZ game"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "Minecraft.Windows.exe").write_bytes(marker)
    (root / "AppxManifest.xml").write_text(
        '<Package><Identity Version="1.26.3301.0" /></Package>',
        encoding="utf-8")
    return root


class EditionListingTests(unittest.TestCase):
    def test_beta_edition_is_hidden_unless_requested(self):
        stable = games.list_editions(include_beta=False)
        everything = games.list_editions(include_beta=True)

        self.assertTrue(stable)
        self.assertTrue(all(not e["beta"] for e in stable))
        self.assertTrue(any(e["beta"] for e in everything))
        self.assertGreater(len(everything), len(stable))


class VersionListingTests(unittest.TestCase):
    def test_builds_already_on_disk_are_marked(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_game(base / "release" / "1.26.42.1")
            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(games.xodus, "version_catalogue",
                                      return_value=_CATALOGUE):
                builds = games.list_versions("release")

        by_version = {b["version"]: b for b in builds}
        # Switching back to a build you already have must cost nothing, so the
        # picker has to be able to say which those are.
        self.assertTrue(by_version["1.26.42.1"]["installed"])
        self.assertFalse(by_version["1.26.44.3"]["installed"])


class InstallTests(unittest.TestCase):
    def setUp(self):
        # install_game() consults the configured game_dir to find a copy
        # inherited from before the Store switch. Without this the tests would
        # read whatever is installed on the machine running them.
        self.settings = {}
        for target, kwargs in (
                ("load_settings", {"side_effect": lambda: dict(self.settings)}),
                ("list_versions", {"return_value": _catalogue()})):
            patcher = mock.patch.object(games, target, **kwargs)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _edition(self):
        return {"id": "release", "product": "9NBLGGH2JHXJ",
                "name": "Minecraft for Windows", "beta": False}

    def test_no_version_named_installs_the_newest(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            def fake_install(url, dest, progress=None):
                _write_game(Path(dest))

            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(games.xodus, "install",
                                      side_effect=fake_install) as install:
                root = games.install_game(self._edition())

            self.assertEqual(root, base / "release" / "1.26.44.3")
            # Every mirror of that build, so a truncated body is retryable.
            self.assertTrue(all("1.26.4403" in url
                                for url in install.call_args[0][0]))
            record = json.loads(
                (base / "release" / "1.26.44.3"
                 / games._INSTALL_METADATA).read_text())
            self.assertEqual(record["version"], "1.26.44.3")

    def test_a_named_version_is_the_one_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            def fake_install(url, dest, progress=None):
                _write_game(Path(dest))

            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(games.xodus, "install",
                                      side_effect=fake_install) as install:
                root = games.install_game(self._edition(), "1.26.42.1")

            self.assertEqual(root, base / "release" / "1.26.42.1")
            self.assertTrue(all("1.26.4201" in url
                                for url in install.call_args[0][0]))

    def test_a_build_already_on_disk_is_not_downloaded_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            _write_game(base / "release" / "1.26.42.1")

            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(games.xodus, "install") as install:
                games.install_game(self._edition(), "1.26.42.1")

            install.assert_not_called()

    def test_a_delisted_version_falls_back_to_the_newest(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            def fake_install(url, dest, progress=None):
                _write_game(Path(dest))

            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(games.xodus, "install",
                                      side_effect=fake_install):
                root = games.install_game(self._edition(), "1.0.0.0")

            # A build Microsoft stopped serving must not leave PLAY dead.
            self.assertEqual(root, base / "release" / "1.26.44.3")

    def test_an_empty_catalogue_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(games, "GAMES", Path(tmp)), \
                mock.patch.object(games, "list_versions", return_value=[]), \
                self.assertRaises(BolError):
            games.install_game(self._edition())

    def test_an_install_from_before_the_store_switch_still_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            # The pre-Store layout is GAMES/<version-tag>/, not
            # GAMES/<edition>/<version>/.
            legacy = _write_game(base / "1.26.42.1" / "Microsoft.MinecraftUWP")
            self.settings["game_dir"] = str(legacy)

            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(
                        games.xodus, "install",
                        side_effect=BolError("downloader not published")):
                root = games.install_game(self._edition())

            # An upgrade must not strand a player on a game they already have.
            self.assertEqual(root, legacy)

    def test_being_signed_out_is_surfaced_not_swallowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            legacy = _write_game(base / "1.26.42.1" / "Microsoft.MinecraftUWP")
            self.settings["game_dir"] = str(legacy)

            # NotSignedIn is a BolError, so the inherited-copy fallback would
            # otherwise eat it and the launcher would keep starting the old
            # build instead of offering the sign-in that would update it.
            with mock.patch.object(games, "GAMES", base), \
                    mock.patch.object(
                        games.xodus, "install",
                        side_effect=games.xodus.NotSignedIn("sign in")), \
                    self.assertRaises(games.xodus.NotSignedIn):
                games.install_game(self._edition())

    def test_a_failed_download_with_nothing_installed_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(games, "GAMES", Path(tmp)), \
                mock.patch.object(games.xodus, "install",
                                  side_effect=BolError("no network")), \
                self.assertRaises(BolError):
            games.install_game(self._edition())

    def test_incomplete_tree_after_streaming_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)

            def truncated(url, dest, progress=None):
                Path(dest).mkdir(parents=True, exist_ok=True)
                (Path(dest) / "Minecraft.Windows.exe").write_bytes(b"MZ")

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
            root = _write_game(games_dir / "preview" / "1.26.50.25")
            settings = {}

            with mock.patch.object(games, "GAMES", games_dir), \
                    mock.patch.object(games, "CONTENT", base / "content"), \
                    mock.patch.object(games, "load_settings",
                                      side_effect=lambda: dict(settings)), \
                    mock.patch.object(games, "save_settings",
                                      side_effect=settings.update):
                games.use_game_dir(root)

        self.assertEqual(settings["mc_edition"], "preview")
        self.assertEqual(settings["mc_version"], "1.26.50.25")

    def test_imported_folder_leaves_the_selection_alone(self):
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
        # Its own build is still reported, from the manifest.
        self.assertEqual(settings["mc_version"], "1.26.33.1")

    def test_auto_selection_prefers_the_remembered_choice(self):
        edition, version = games._auto_selection(
            {"mc_edition": "preview", "mc_version": "1.26.50.25"})
        self.assertEqual(edition["id"], "preview")
        self.assertEqual(version, "1.26.50.25")

    def test_auto_selection_falls_back_to_the_stable_edition(self):
        edition, version = games._auto_selection({})
        self.assertFalse(edition["beta"])
        # No version means "whatever is newest", which install_game resolves.
        self.assertIsNone(version)


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

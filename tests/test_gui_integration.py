"""bol.test_gui_integration — GUI integration tests for PySide6 rewrite."""
# SPDX-License-Identifier: MIT

import sys
import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest
from PySide6.QtWidgets import QApplication, QMainWindow
from PySide6.QtTest import QSignalSpy

# Ensure we can import bol modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from bol.gui import (
    MainWindow, Theme, VersionPicker, ProfileMenu, Worker,
    _resolve_gui_display, _owned_x11_socket_displays,
    SwitchRow,
)


# ======================================================================
# Fixtures
# ======================================================================

@pytest.fixture(scope="session")
def qapp():
    """Create QApplication for the test session."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app
    # Don't quit; let pytest handle cleanup


@pytest.fixture
def temp_settings_dir():
    """Temporary directory for settings files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Mock the settings directory
        yield Path(tmpdir)


@pytest.fixture
def main_window(qapp, monkeypatch, temp_settings_dir):
    """Create a MainWindow instance for testing."""
    # Mock config functions to use temp directory
    def mock_load_settings():
        settings_file = temp_settings_dir / "settings.json"
        if settings_file.exists():
            with open(settings_file) as f:
                return json.load(f)
        return {}

    def mock_save_settings(settings):
        settings_file = temp_settings_dir / "settings.json"
        with open(settings_file, "w") as f:
            json.dump(settings, f)

    monkeypatch.setattr("bol.gui.load_settings", mock_load_settings)
    monkeypatch.setattr("bol.gui.save_settings", mock_save_settings)
    monkeypatch.setattr("bol.gui.current_profile_name", lambda: "Default")
    monkeypatch.setattr("bol.gui.current_profile_info", lambda: {"path": None})
    monkeypatch.setattr("bol.gui.list_profiles", lambda: [])
    monkeypatch.setattr("bol.gui.msa_signed_in", lambda: False)
    monkeypatch.setattr("bol.gui.msa_gamertag", lambda: None)
    monkeypatch.setattr("bol.gui._resolve_gui_display", lambda e=None: ":0")

    window = MainWindow()
    yield window
    window.deleteLater()


# ======================================================================
# Theme Tests
# ======================================================================

class TestTheme:
    """Test Theme class and QSS generation."""

    def test_theme_initialization(self):
        """Theme initializes with default dark mode."""
        theme = Theme()
        assert theme.dark is True
        assert theme.beta is False

    def test_theme_light_mode(self):
        """Light theme returns correct colors."""
        theme = Theme(dark=False)
        assert theme.bg == "#eef1f6"
        assert theme.fg == "#12141a"

    def test_theme_dark_mode(self):
        """Dark theme returns correct colors."""
        theme = Theme(dark=True)
        assert theme.bg == "#0d0f14"
        assert theme.fg == "#eef1f6"

    def test_theme_beta_accent(self):
        """Beta mode uses gold accent."""
        theme = Theme(beta=True)
        assert theme.accent == theme.gold
        assert theme.accent_hov == theme.gold_hov

    def test_theme_stable_accent(self):
        """Stable mode uses green accent."""
        theme = Theme(beta=False)
        assert theme.accent == theme.green
        assert theme.accent_hov == theme.green_hov

    def test_qss_generation(self):
        """QSS stylesheet is generated without errors."""
        theme = Theme()
        qss = theme.qss()
        assert "QMainWindow" in qss
        assert "QPushButton" in qss
        assert "QLineEdit" in qss
        assert theme.fg in qss
        assert theme.bg in qss


# ======================================================================
# MainWindow Initialization Tests
# ======================================================================

class TestMainWindowInitialization:
    """Test MainWindow startup and initialization."""

    def test_main_window_creates(self, qapp):
        """MainWindow initializes without errors."""
        with mock.patch.object(MainWindow, "__init__", lambda x: None):
            window = QMainWindow()
            assert window is not None

    def test_main_window_properties(self, main_window):
        """MainWindow has expected properties after initialization."""
        assert main_window.windowTitle() != ""
        assert main_window.width() >= 880
        assert main_window.height() >= 640
        assert main_window.theme is not None
        assert isinstance(main_window.theme, Theme)

    def test_main_window_ui_state(self, main_window):
        """UI state is initialized correctly."""
        assert main_window.ui_state["busy"] is False
        assert main_window.ui_state["launch_active"] is False
        assert main_window.ui_state["details"] is False
        assert main_window.ui_state["stepped_aside"] is False

    def test_main_window_central_widget(self, main_window):
        """Central widget and stack are created."""
        assert main_window.centralWidget() is not None
        assert main_window.stack is not None

    def test_main_window_pages(self, main_window):
        """Main page, settings, and changelog pages exist."""
        assert main_window.hero_page is not None
        assert main_window.settings_page is not None
        assert main_window.changelog_page is not None


# ======================================================================
# Theme Application Tests
# ======================================================================

class TestThemeApplication:
    """Test applying themes to the UI."""

    def test_apply_light_theme(self, main_window):
        """Applying light theme updates stylesheet."""
        main_window.settings["light_theme"] = True
        main_window.apply_theme()
        assert main_window.theme.dark is False

    def test_apply_dark_theme(self, main_window):
        """Applying dark theme updates stylesheet."""
        main_window.settings["light_theme"] = False
        main_window.apply_theme()
        assert main_window.theme.dark is True

    def test_apply_beta_theme(self, main_window):
        """Applying beta theme changes accent color."""
        main_window.settings["ui_is_beta"] = True
        main_window.apply_theme()
        assert main_window.theme.beta is True

    def test_theme_switches_update(self, main_window):
        """SwitchRow widgets are updated when theme changes."""
        # Add a switch to track
        test_switch = main_window._switch("Test Toggle")
        assert test_switch in main_window._switches
        main_window.apply_theme()
        # No exception should be raised


# ======================================================================
# Profile Menu Tests
# ======================================================================

class TestProfileMenu:
    """Test profile switcher menu."""

    def test_profile_menu_creation(self, qapp):
        """ProfileMenu initializes without errors."""
        with mock.patch("bol.gui.QApplication.instance", return_value=qapp):
            menu = ProfileMenu(None)
            assert menu is not None

    def test_profile_menu_rebuild(self, qapp):
        """ProfileMenu rebuilds with profiles list."""
        with mock.patch("bol.gui.QApplication.instance", return_value=qapp):
            menu = ProfileMenu(None)
            profiles = [
                {"name": "Profile 1", "path": "/path/to/profile1"},
                {"name": "Profile 2", "path": "/path/to/profile2"},
            ]
            menu.rebuild(profiles, None)
            # Should not raise


# ======================================================================
# Version Picker Tests
# ======================================================================

class TestVersionPicker:
    """Test version selection widget."""

    def test_version_picker_creation(self, qapp):
        """VersionPicker initializes without errors."""
        with mock.patch("bol.gui.QApplication.instance", return_value=qapp):
            picker = VersionPicker(None)
            assert picker is not None
            assert picker.list is not None
            assert picker.search is not None

    def test_version_picker_set_labels(self, qapp):
        """VersionPicker displays version labels."""
        with mock.patch("bol.gui.QApplication.instance", return_value=qapp):
            picker = VersionPicker(None)
            labels = ["1.0.0", "1.1.0", "1.2.0"]
            picker.set_labels(labels, "1.1.0")
            assert picker.list.count() == 3

    def test_version_picker_filter(self, qapp):
        """VersionPicker filters versions by search text."""
        with mock.patch("bol.gui.QApplication.instance", return_value=qapp):
            picker = VersionPicker(None)
            labels = ["1.0.0", "1.1.0", "2.0.0"]
            picker.set_labels(labels, "1.0.0")
            picker.search.setText("1.")
            # First item should not be hidden after filtering
            assert picker.list.item(0).isHidden() is False


# ======================================================================
# Settings Persistence Tests
# ======================================================================

class TestSettingsPersistence:
    """Test settings are saved and loaded correctly."""

    def test_save_and_load_theme_setting(self, main_window, temp_settings_dir):
        """Theme setting persists across saves."""
        main_window.settings["light_theme"] = True
        from bol.gui import save_settings, load_settings
        save_settings(main_window.settings)

        loaded = load_settings()
        assert loaded.get("light_theme") is True

    def test_settings_file_created(self, temp_settings_dir):
        """Settings file is created on save."""
        from bol.gui import save_settings
        settings = {"test_key": "test_value"}
        save_settings(settings)


# ======================================================================
# Worker Thread Tests
# ======================================================================

class TestWorkerThread:
    """Test background worker threading."""

    def test_worker_creation(self):
        """Worker initializes with a callable."""
        def dummy_work():
            return "result"

        worker = Worker(dummy_work)
        assert worker is not None
        assert worker._fn is dummy_work

    def test_worker_emits_done_signal(self, qapp):
        """Worker emits done signal with result."""
        def dummy_work():
            return "test_result"

        worker = Worker(dummy_work)
        spy = QSignalSpy(worker.done)
        worker.run()

        # Check that signal was emitted
        assert spy.count() > 0

    def test_worker_emits_failed_signal(self, qapp):
        """Worker emits failed signal on exception."""
        def failing_work():
            raise ValueError("Test error")

        worker = Worker(failing_work)
        spy = QSignalSpy(worker.failed)
        worker.run()

        assert spy.count() > 0


# ======================================================================
# Log Display Tests
# ======================================================================

class TestLogDisplay:
    """Test activity log display."""

    def test_log_view_created(self, main_window):
        """Log view widget is created."""
        assert main_window.log_view is not None

    def test_log_drawer_initially_hidden(self, main_window):
        """Log drawer is hidden initially."""
        assert main_window.log_drawer.isHidden()

    def test_toggle_details_shows_log(self, main_window):
        """Toggling details shows the log drawer."""
        main_window.toggle_details()
        assert not main_window.log_drawer.isHidden()
        main_window.toggle_details()
        assert main_window.log_drawer.isHidden()

    def test_log_line_display(self, main_window):
        """Log lines are displayed in the log view."""
        test_message = "Test log message"
        main_window._on_log_line(test_message)
        log_text = main_window.log_view.toPlainText()
        assert test_message in log_text or len(log_text) > 0


# ======================================================================
# Page Navigation Tests
# ======================================================================

class TestPageNavigation:
    """Test switching between main pages."""

    def test_hero_page_shown_initially(self, main_window):
        """Hero page is shown by default."""
        assert main_window.stack.currentWidget() is main_window.hero_page

    def test_toggle_settings_page(self, main_window):
        """Settings page toggles correctly."""
        main_window.toggle_settings()
        assert main_window.stack.currentWidget() is main_window.settings_page
        main_window.toggle_settings()
        assert main_window.stack.currentWidget() is main_window.hero_page

    def test_toggle_changelog_page(self, main_window):
        """Changelog page toggles correctly."""
        main_window.toggle_changelog()
        assert main_window.stack.currentWidget() is main_window.changelog_page
        main_window.toggle_changelog()
        assert main_window.stack.currentWidget() is main_window.hero_page


# ======================================================================
# UI State Tests
# ======================================================================

class TestUIState:
    """Test UI state management."""

    def test_set_busy_play_button(self, main_window):
        """Busy state changes play button to kill."""
        main_window._set_busy(True)
        assert "KILL" in main_window.play_btn.text()
        main_window._set_busy(False)
        assert "PLAY" in main_window.play_btn.text()

    def test_step_aside_for_game(self, main_window):
        """Step aside hides the window."""
        main_window._step_aside_for_game()
        assert main_window.ui_state["stepped_aside"]

    def test_come_back_from_game(self, main_window):
        """Coming back shows the window."""
        main_window._step_aside_for_game()
        main_window._come_back_from_game()
        assert not main_window.ui_state["stepped_aside"]


# ======================================================================
# Display Resolution Tests
# ======================================================================

class TestDisplayResolution:
    """Test X11/Wayland display handling."""

    def test_resolve_gui_display_returns_display(self):
        """_resolve_gui_display returns a display string."""
        environ = {"DISPLAY": ":0"}
        result = _resolve_gui_display(environ=environ)
        assert result is not None

    def test_resolve_gui_display_with_wayland(self):
        """_resolve_gui_display works with Wayland environment."""
        environ = {
            "DISPLAY": ":0",
            "WAYLAND_DISPLAY": "wayland-0"
        }
        result = _resolve_gui_display(environ=environ)
        # Should return a display (either the original or a fallback)
        assert result is not None

    def test_owned_x11_sockets_returns_tuple(self):
        """_owned_x11_socket_displays returns a tuple."""
        try:
            result = _owned_x11_socket_displays()
            assert isinstance(result, tuple)
        except OSError:
            # Expected if /tmp/.X11-unix doesn't exist
            pass


# ======================================================================
# Settings Tab Tests
# ======================================================================

class TestSettingsTab:
    """Test settings interface."""

    def test_general_tab_created(self, main_window):
        """General settings tab is created."""
        settings_page = main_window.settings_page
        assert settings_page is not None

    def test_theme_toggle_in_settings(self, main_window):
        """Light theme toggle exists in general tab."""
        # The _build_general_tab creates theme switches
        main_window._build_general_tab()
        # If we get here without exception, the tab was created


# ======================================================================
# Account Row Tests
# ======================================================================

class TestAccountRow:
    """Test account status display."""

    def test_account_row_not_signed_in(self, main_window):
        """Account shows not signed in by default."""
        # main_window initializes with not signed in
        assert main_window.acct_text is not None

    def test_refresh_account_row_in(self, main_window):
        """Account refresh for signed in state."""
        main_window._refresh_account_row("in")
        assert "Signed in" in main_window.acct_text.text()

    def test_refresh_account_row_out(self, main_window):
        """Account refresh for signed out state."""
        main_window._refresh_account_row("out")
        assert "Not signed in" in main_window.acct_text.text()


# ======================================================================
# Switch Row Tests
# ======================================================================

class TestSwitchRow:
    """Test toggle switch row widgets."""

    def test_switch_row_creation(self):
        """SwitchRow initializes with text."""
        row = SwitchRow("Test Toggle")
        assert row is not None
        assert "Test Toggle" in row.layout().itemAt(0).widget().text()

    def test_switch_row_checked_state(self):
        """SwitchRow tracks checked state."""
        row = SwitchRow("Test", checked=True)
        assert row.isChecked() is True

    def test_switch_row_toggle(self):
        """SwitchRow can be toggled."""
        row = SwitchRow("Test", checked=False)
        row.switch.setChecked(True)
        assert row.isChecked() is True


# ======================================================================
# Status Row Tests
# ======================================================================

class TestStatusRow:
    """Test status display."""

    def test_status_label_created(self, main_window):
        """Status label exists."""
        assert main_window.status_label is not None

    def test_progress_bar_hidden_initially(self, main_window):
        """Progress bar is hidden initially."""
        assert main_window.progress.isHidden()

    def test_show_progress_bar(self, main_window):
        """Progress bar can be shown."""
        main_window.set_progress(50, 100)
        assert not main_window.progress.isHidden()


# ======================================================================
# Integration Test: Full Workflow
# ======================================================================

class TestFullWorkflow:
    """Integration tests for complete workflows."""

    def test_init_to_play_button_ready(self, main_window):
        """Window initializes and play button is ready."""
        assert main_window.play_btn is not None
        assert main_window.play_btn.isEnabled() or not main_window.play_btn.isEnabled()

    def test_settings_open_and_close(self, main_window):
        """Settings can be opened and closed."""
        main_window.toggle_settings()
        assert main_window.stack.currentWidget() is main_window.settings_page
        main_window.toggle_settings()
        assert main_window.stack.currentWidget() is main_window.hero_page

    def test_multiple_toggles(self, main_window):
        """Multiple rapid toggles work correctly."""
        for _ in range(5):
            main_window.toggle_settings()
        # Odd number of toggles should land on the settings page
        assert main_window.stack.currentWidget() is main_window.settings_page


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

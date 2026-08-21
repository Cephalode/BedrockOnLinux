"""The launcher window's lifetime, and the process's lifetime behind it.

These two are deliberately not the same thing. `setQuitOnLastWindowClosed`
is off so that "close the launcher when Minecraft starts" can take the window
away while the launch thread keeps supervising the game -- a session nobody
watches out leaves the GPU safety marker armed and blocks the next launch.
The cost of that setting is that quitting becomes an explicit act, and these
tests hold both halves of the bargain: an ordinary close ends the process, a
close-for-the-game does not.
"""
# SPDX-License-Identifier: MIT

import os
import unittest
from contextlib import contextmanager
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from bol import gui, log


def _app():
    app = QApplication.instance()
    if app is None:
        app = QApplication(["bedrock-on-linux-tests"])
    # gui() sets this, and it is the whole reason these tests exist.
    app.setQuitOnLastWindowClosed(False)
    return app


@contextmanager
def headless_window(**settings):
    """A real MainWindow with every outward-facing call stubbed.

    __init__ arms singleShot timers for refresh_versions and the update check;
    any test that runs the event loop would otherwise reach the network, so
    they are patched out rather than merely left unrun.
    """
    saved_sink = log._LOG_SINK
    with mock.patch.object(gui, "NativeAuth"), \
            mock.patch.object(gui, "msa_signed_in", return_value=False), \
            mock.patch.object(gui, "msa_gamertag", return_value=None), \
            mock.patch.object(gui, "current_profile_name", return_value="Default"), \
            mock.patch.object(gui, "load_settings", lambda: dict(settings)), \
            mock.patch.object(gui, "save_settings", lambda _s: None), \
            mock.patch.object(gui.MainWindow, "refresh_versions", lambda _s: None), \
            mock.patch.object(gui.MainWindow, "check_for_update_async", lambda _s: None), \
            mock.patch.object(gui.MainWindow, "_refresh_store_row", lambda _s: None):
        window = gui.MainWindow()
        try:
            yield window
        finally:
            window._force_close = True
            window.close()
            window.deleteLater()
            log._LOG_SINK = saved_sink


def run_loop_until_quit(app, timeout_ms=3000):
    """Run the event loop and report whether it ended on its own.

    Returns True when something called quit() before the watchdog fired.
    """
    timed_out = []
    watchdog = QTimer()
    watchdog.setSingleShot(True)
    watchdog.timeout.connect(lambda: (timed_out.append(True), app.quit()))
    watchdog.start(timeout_ms)
    app.exec()
    watchdog.stop()
    return not timed_out


class ClosingTheWindowEndsTheProcessTests(unittest.TestCase):
    def test_closing_the_window_quits_the_application(self):
        app = _app()
        with headless_window() as window:
            window.show()
            QTimer.singleShot(0, window.close)
            self.assertTrue(
                run_loop_until_quit(app),
                "closing the launcher left the event loop running: with "
                "setQuitOnLastWindowClosed(False) the process outlives its "
                "own window unless closeEvent quits explicitly")

    def test_closing_for_the_game_leaves_the_loop_running(self):
        # The mirror image: the window goes, the launch thread keeps the
        # process alive until it reports back.
        app = _app()
        with headless_window(close_on_launch=True) as window:
            window.show()
            window.ui_state["launch_active"] = True
            QTimer.singleShot(0, window._close_for_game)
            self.assertFalse(
                run_loop_until_quit(app, timeout_ms=600),
                "the launcher quit while it was still supervising the game")
            self.assertTrue(window.ui_state["window_gone"])

    def test_the_supervising_thread_quits_once_the_game_is_gone(self):
        app = _app()
        with headless_window(close_on_launch=True) as window:
            window.show()
            window.ui_state["launch_active"] = True
            window._close_for_game()
            QTimer.singleShot(0, lambda: window._play_finished("closed"))
            self.assertTrue(
                run_loop_until_quit(app),
                "the window was gone and the game had exited, but nothing "
                "ended the process")


class RefusingToCloseTests(unittest.TestCase):
    """The guards are about not orphaning work, so they must not be
    reachable by the close-for-the-game path (which sets _force_close)."""

    def test_a_running_game_refuses_the_close(self):
        _app()
        with headless_window() as window:
            window.show()
            window.ui_state["launch_active"] = True
            with mock.patch.object(window, "warn_box") as warned:
                window.close()
            self.assertTrue(warned.called)
            self.assertTrue(window.isVisible(),
                            "the close was refused but the window went away")

    def test_a_busy_preparation_refuses_the_close(self):
        _app()
        with headless_window() as window:
            window.ui_state["busy"] = True
            with mock.patch.object(window, "warn_box") as warned:
                window.close()
            self.assertTrue(warned.called)

    def test_closing_for_the_game_bypasses_both_guards(self):
        _app()
        with headless_window(close_on_launch=True) as window:
            window.ui_state["launch_active"] = True
            window.ui_state["busy"] = True
            with mock.patch.object(window, "warn_box") as warned:
                window._close_for_game()
            self.assertFalse(
                warned.called,
                "the close-on-launch path hit the 'Minecraft is running' "
                "guard it is supposed to be exempt from")


if __name__ == "__main__":
    unittest.main()

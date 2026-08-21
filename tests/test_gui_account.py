"""The account row: one sign-in at a time, and questions that expire.

Two of the three buttons in this row are destructive and ask before acting,
which only works if the question can also be withdrawn -- otherwise it sits
armed on screen and the next click anywhere answers it. The third starts a
device-code flow that must not be started twice.
"""
# SPDX-License-Identifier: MIT

import unittest
from unittest import mock

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication

from bol import gui
from tests.guiharness import headless_window, qt_app


def _click_on(widget):
    return QMouseEvent(QEvent.MouseButtonPress, QPointF(1, 1), QPointF(1, 1),
                       Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)


class SignInIsStartedOnceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        qt_app()

    def test_a_second_click_does_not_start_a_second_flow(self):
        # Every start() is a fresh device code; two in flight means the
        # player is shown one code while the launcher waits on another.
        with headless_window() as window:
            started = []
            window.na.start = lambda *a, **k: started.append(a)
            with mock.patch.object(gui.threading, "Thread") as thread:
                thread.side_effect = lambda target, daemon=None: mock.Mock(
                    start=target)
                window.acct_click()
                window.acct_click()
                window.acct_click()
            self.assertEqual(thread.call_count, 1)

    def test_the_button_says_it_is_working(self):
        with headless_window() as window:
            with mock.patch.object(gui.threading, "Thread"):
                window.acct_click()
            self.assertEqual(window.acct_btn.text(), "Loading…")
            self.assertEqual(window._acct_mode, "loading")

    def test_the_code_arriving_makes_the_button_a_cancel(self):
        with headless_window() as window:
            with mock.patch.object(gui.threading, "Thread"):
                window.acct_click()
            window._refresh_account_row("auth")
            self.assertEqual(window._acct_mode, "cancel")
            self.assertEqual(window.acct_btn.text(), "Cancel")


class ConfirmationsCanBeWithdrawnTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        qt_app()

    def test_arming_sign_out_asks_first(self):
        with headless_window() as window:
            window._refresh_account_row("in")
            window.acct_click()
            self.assertEqual(window.acct_btn.text(), "Sign out?")
            self.assertTrue(window._acct_confirm)

    def test_confirming_signs_out(self):
        with headless_window() as window:
            window._refresh_account_row("in")
            window.acct_click()
            with mock.patch.object(gui, "msa_logout") as logout, \
                    mock.patch.object(gui, "msa_signed_in", return_value=False):
                window.acct_click()
            self.assertTrue(logout.called)

    def test_a_click_elsewhere_withdraws_the_question(self):
        with headless_window() as window:
            window.show()
            window._refresh_account_row("in")
            window.acct_click()
            self.assertTrue(window._acct_confirm)
            QApplication.instance().sendEvent(
                window.play_btn, _click_on(window.play_btn))
            self.assertFalse(window._acct_confirm)
            self.assertEqual(window.acct_btn.text(), "Sign out")

    def test_a_click_on_the_button_itself_does_not_withdraw_it(self):
        with headless_window() as window:
            window.show()
            window._refresh_account_row("in")
            window.acct_click()
            QApplication.instance().sendEvent(
                window.acct_btn, _click_on(window.acct_btn))
            self.assertTrue(window._acct_confirm)


class TheFilterIsNotLeftInstalledTests(unittest.TestCase):
    """An application event filter is consulted for every event delivered
    anywhere in the process, so one left behind costs the whole app -- and
    one per window costs it that many times over."""

    @classmethod
    def setUpClass(cls):
        qt_app()

    def test_nothing_is_watched_while_no_question_is_up(self):
        with headless_window() as window:
            self.assertFalse(getattr(window, "_watching_clicks", False))

    def test_withdrawing_stops_watching(self):
        with headless_window() as window:
            window._refresh_account_row("in")
            window.acct_click()
            self.assertTrue(window._watching_clicks)
            window._disarm_account_confirm()
            self.assertFalse(window._watching_clicks)

    def test_answering_stops_watching(self):
        with headless_window() as window:
            window._refresh_account_row("in")
            window.acct_click()
            with mock.patch.object(gui, "msa_logout"), \
                    mock.patch.object(gui, "msa_signed_in", return_value=False):
                window.acct_click()
            self.assertFalse(window._watching_clicks)

    def test_closing_stops_watching(self):
        with headless_window() as window:
            window.show()
            window._refresh_account_row("in")
            window.acct_click()
            window.close()
            self.assertFalse(window._watching_clicks)


class XboxPreauthWarmUpTests(unittest.TestCase):
    """launch.py runs xbl_preauth again at PLAY, so this is a warm-up, not a
    dependency -- but dropping it moves the whole SISU/XSTS round trip onto
    the first launch instead of the sign-in the player is already waiting on."""

    @classmethod
    def setUpClass(cls):
        qt_app()

    def _run_warm_up(self, window):
        with mock.patch.object(gui.threading, "Thread") as thread:
            thread.side_effect = lambda target, daemon=None: mock.Mock(
                start=target)
            window._warm_xbox_preauth()

    def test_coming_online_warms_the_token_chain(self):
        with headless_window() as window:
            with mock.patch.object(window, "_warm_xbox_preauth") as warm:
                window._on_online()
            self.assertTrue(warm.called)

    def test_the_refreshed_token_is_handed_to_xbl_preauth(self):
        from bol import auth
        with headless_window() as window:
            with mock.patch.object(auth, "msa_load",
                                   return_value={"refresh_token": "r"}), \
                    mock.patch.object(auth, "msa_refresh",
                                      return_value={"access_token": "a"}), \
                    mock.patch.object(auth, "_account_cache_epoch",
                                      return_value=7), \
                    mock.patch.object(auth, "xbl_preauth",
                                      return_value=True) as preauth:
                self._run_warm_up(window)
            preauth.assert_called_once_with("a", 7)

    def test_no_stored_account_is_not_an_error(self):
        from bol import auth
        with headless_window() as window:
            with mock.patch.object(auth, "msa_load", return_value=None), \
                    mock.patch.object(auth, "xbl_preauth") as preauth:
                self._run_warm_up(window)
            self.assertFalse(preauth.called)

    def test_a_failing_warm_up_is_swallowed(self):
        # PLAY re-runs the chain and is where a real failure gets reported,
        # with its diagnostic attached. A warm-up must never surface as a
        # crash in a background thread.
        from bol import auth
        with headless_window() as window:
            with mock.patch.object(auth, "msa_load",
                                   side_effect=RuntimeError("offline")):
                self._run_warm_up(window)


if __name__ == "__main__":
    unittest.main()

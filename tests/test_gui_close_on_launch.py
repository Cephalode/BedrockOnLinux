"""Closing the launcher when the game starts, without abandoning the session.

The window is the only thing the setting takes away. The process behind it
still has to see the launch out: it armed a GPU safety marker before starting
the game, and a marker nobody watches return blocks the next launch until a
reboot. These tests pin the decision and the wiring that keeps that true.
"""
# SPDX-License-Identifier: MIT

import ast
import inspect
import unittest
from pathlib import Path

from bol import gui
from bol.gui import window_action_for_launch


class WindowActionTests(unittest.TestCase):
    def test_an_ordinary_desktop_keeps_both_windows(self):
        self.assertEqual(window_action_for_launch({}, False), "stay")

    def test_the_setting_is_off_until_it_is_turned_on(self):
        # Settings written before the switch existed carry no key at all.
        self.assertEqual(window_action_for_launch({"light_theme": True}, False),
                         "stay")
        self.assertEqual(window_action_for_launch(None, False), "stay")

    def test_a_single_window_session_steps_aside_on_its_own(self):
        self.assertEqual(window_action_for_launch({}, True), "step-aside")

    def test_the_setting_closes_the_window(self):
        self.assertEqual(
            window_action_for_launch({"close_on_launch": True}, False), "close")

    def test_closing_wins_over_stepping_aside(self):
        # Both take the window off the screen; only one was asked for, and
        # coming back afterwards would contradict it.
        self.assertEqual(
            window_action_for_launch({"close_on_launch": True}, True), "close")

    def test_the_setting_being_off_still_steps_aside_in_game_mode(self):
        self.assertEqual(
            window_action_for_launch({"close_on_launch": False}, True),
            "step-aside")


def _nested_function(tree, name):
    """The function called ``name``, or ``outer.inner`` for a nested one.

    gui() defines several workers called ``work``; naming the one that
    launches the game keeps these tests pinned to it.
    """
    outer, _, inner = name.rpartition(".")
    scope = _nested_function(tree, outer) if outer else tree
    if scope is None:
        return None
    for node in ast.walk(scope):
        if isinstance(node, ast.FunctionDef) and node.name == inner:
            return node
    return None


class ClosedWindowWiringTests(unittest.TestCase):
    """gui() is one long closure, so its wiring is read rather than run."""

    def setUp(self):
        source = Path(inspect.getsourcefile(gui)).read_text(encoding="utf-8")
        self.tree = ast.parse(source)

    def _calls_in(self, function_name):
        node = _nested_function(self.tree, function_name)
        self.assertIsNotNone(node, f"{function_name} not found in bol.gui")
        return {
            call.func.id for call in ast.walk(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
        }

    def _attribute_calls_in(self, function_name):
        node = _nested_function(self.tree, function_name)
        self.assertIsNotNone(node, f"{function_name} not found in bol.gui")
        return {
            call.func.attr for call in ast.walk(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
        }

    def test_the_launch_thread_decides_with_the_shared_helper(self):
        self.assertIn("window_action_for_launch", self._calls_in("do_play.work"))

    def test_the_game_start_hook_can_close_or_step_aside(self):
        hook = self._calls_in("window_steps_out_for_game")
        self.assertIn("close_for_game", hook)
        self.assertIn("step_aside_for_game", hook)

    def test_closing_destroys_the_window_and_stops_the_auth_poller(self):
        shut = self._attribute_calls_in("close_for_game.shut")
        self.assertIn("destroy", shut)
        self.assertIn("stop", shut)

    def test_nothing_the_launch_thread_reports_goes_straight_to_tk(self):
        # A destroyed window cannot schedule work, and the thread raising on
        # that would abandon the game it is still supervising. Everything
        # goes through ui_after, which drops the update instead.
        for name in ("set_status", "bar_busy", "set_progress", "end_progress",
                     "step_aside_for_game", "come_back_from_game", "do_play.work"):
            with self.subTest(function=name):
                node = _nested_function(self.tree, name)
                self.assertIsNotNone(node, f"{name} not found in bol.gui")
                scheduled = {
                    call.func.attr for call in ast.walk(node)
                    if isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and getattr(call.func.value, "id", None) == "root"
                }
                self.assertNotIn("after", scheduled)

    def test_a_failure_with_no_window_left_is_still_reported(self):
        self.assertIn("desktop_notify", self._calls_in("do_play.work"))


if __name__ == "__main__":
    unittest.main()

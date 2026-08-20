"""Tests for the controller focus ring."""
# SPDX-License-Identifier: MIT

import unittest

from bol.navigation import (
    ControllerNav,
    choose_neighbour,
    reading_order,
    within,
)


class Bindings:
    """A stand-in for the canvas customtkinter hides inside every widget."""

    def __init__(self, *sequences):
        self.bindings = tuple(sequences)
        self.events = []

    def event_generate(self, sequence, **kwargs):
        self.events.append(sequence)


class CTkBaseClass:
    pass


class Widget(CTkBaseClass):
    """Enough of a Tk widget for the ring to walk, measure and light up."""

    def __init__(self, class_name, box=(0, 0, 100, 30), children=(),
                 clickable=False, state="normal", parent=None,
                 mapped=True, options=None, scrollregion=None):
        self.__class__ = type(class_name, (Widget,), {})
        self.box = box
        self.master = parent
        self.mapped = mapped
        self.state = state
        self.invoked = 0
        self.toggled = 0
        self.focused = 0
        self.options = {"border_width": 0, "border_color": "#333"}
        self.options.update(options or {})
        self._children = list(children)
        for child in self._children:
            child.master = self
        if clickable:
            self._canvas = Bindings("<Button-1>")
        self.scrollregion = scrollregion
        self.view = [0.0, 1.0]
        self.scheduled = []

    # -- geometry
    def winfo_children(self):
        return list(self._children)

    def winfo_ismapped(self):
        return self.mapped

    def winfo_rootx(self):
        return self.box[0]

    def winfo_rooty(self):
        return self.box[1]

    def winfo_width(self):
        return self.box[2]

    def winfo_height(self):
        return self.box[3]

    def winfo_exists(self):
        return True

    def winfo_toplevel(self):
        node = self
        while node.master is not None:
            node = node.master
        return node

    # -- options
    def cget(self, option):
        if option == "state":
            return self.state
        if option == "scrollregion":
            if self.scrollregion is None:
                raise ValueError("no scrollregion")
            return self.scrollregion
        if option in self.options:
            return self.options[option]
        raise ValueError(f"{option} is not supported")

    def configure(self, **kwargs):
        for option, value in kwargs.items():
            if option not in self.options:
                raise ValueError(f"{option} is not supported")
            self.options[option] = value

    # -- behaviour
    def invoke(self):
        self.invoked += 1

    def toggle(self):
        self.toggled += 1

    def focus_set(self):
        self.focused += 1

    # -- scrolling
    def yview(self):
        return tuple(self.view)

    def yview_moveto(self, fraction):
        self.view = [max(0.0, min(1.0, fraction)), self.view[1]]

    def grab_current(self):
        return None

    def after(self, _delay, callback=None):
        # Recorded, not run: the pump reschedules itself from inside itself,
        # and a test double that runs it immediately would recurse.
        self.scheduled.append(callback)
        return "after#1"

    def after_cancel(self, _job):
        pass

    def after_idle(self, callback):
        callback()

    def update_idletasks(self):
        pass


class FakeTk:
    class Toplevel:
        pass

    class Misc:
        @staticmethod
        def bind(widget, *_args, **_kwargs):
            return getattr(widget, "bindings", ())


class FakeCtk:
    CTkBaseClass = CTkBaseClass


def button(box, **kwargs):
    return Widget("CTkButton", box, **kwargs)


def navigator(root, **kwargs):
    return ControllerNav(root, FakeCtk, FakeTk, accent="#3DDC84", **kwargs)


class GeometryTests(unittest.TestCase):
    def test_within_needs_an_overlap_on_both_axes(self):
        view = (0, 0, 100, 100)
        self.assertTrue(within((10, 10, 20, 20), view))
        self.assertFalse(within((10, 200, 20, 20), view))
        self.assertTrue(within((10, 130, 20, 20), view, margin=48))
        self.assertFalse(within((10, 200, 20, 20), view, margin=48))

    def test_reading_order_is_down_then_across(self):
        items = ["a", "b", "c"]
        rects = [(500, 10, 10, 10), (10, 10, 10, 10), (10, 5, 10, 10)]
        self.assertEqual(reading_order(items, rects), ["c", "b", "a"])


class NeighbourTests(unittest.TestCase):
    # PLAY, the gear and Details, the way they sit along the dock.
    ROW = [(882, 690, 110, 52), (815, 690, 52, 52), (729, 690, 78, 52)]

    def test_the_nearest_control_in_that_direction_wins(self):
        self.assertEqual(choose_neighbour(self.ROW, 0, "left"), 1)
        self.assertEqual(choose_neighbour(self.ROW, 1, "left"), 2)
        self.assertEqual(choose_neighbour(self.ROW, 2, "right"), 1)

    def test_an_aligned_control_beats_a_closer_but_offset_one(self):
        rects = [
            (100, 100, 80, 30),    # current
            (400, 108, 80, 30),    # same row, further away
            (260, 260, 80, 30),    # nearer as the crow flies, way off the row
        ]
        self.assertEqual(choose_neighbour(rects, 0, "right"), 1)

    def test_nothing_ahead_wraps_to_the_far_side(self):
        self.assertEqual(choose_neighbour(self.ROW, 2, "left"), 0)
        self.assertEqual(choose_neighbour(self.ROW, 0, "right"), 2)

    def test_wrapping_can_be_turned_off(self):
        self.assertIsNone(choose_neighbour(self.ROW, 2, "left", wrap=False))
        self.assertEqual(choose_neighbour(self.ROW, 0, "left", wrap=False), 1)

    def test_vertical_movement_prefers_the_column(self):
        rects = [
            (900, 150, 80, 30),    # current, top right
            (890, 220, 80, 30),    # straight below
            (120, 200, 80, 30),    # closer vertically, far to the left
        ]
        self.assertEqual(choose_neighbour(rects, 0, "down"), 1)

    def test_an_empty_screen_has_no_neighbour(self):
        self.assertIsNone(choose_neighbour([], None, "down"))
        self.assertEqual(choose_neighbour(self.ROW, None, "down"), 0)


class CollectionTests(unittest.TestCase):
    def test_controls_are_found_and_decoration_is_not(self):
        play = button((880, 690, 110, 52))
        label = Widget("CTkLabel", (100, 100, 80, 20))
        root = Widget("CTk", (0, 0, 1000, 800), children=[
            Widget("CTkFrame", (0, 0, 1000, 60), children=[label]),
            Widget("CTkFrame", (0, 660, 1000, 100), children=[play]),
        ])
        nav = navigator(root)
        items, _rects = nav._items()
        self.assertEqual(items, [play])

    def test_a_clickable_frame_counts_once_instead_of_its_labels(self):
        # The version pill: a frame with two labels in it, all three wired to
        # the same click. To a controller that is one control.
        inner = [Widget("CTkLabel", (100, 700, 60, 20), clickable=True),
                 Widget("CTkLabel", (170, 700, 10, 20), clickable=True)]
        pill = Widget("CTkFrame", (88, 690, 220, 52), children=inner,
                      clickable=True)
        root = Widget("CTk", (0, 0, 1000, 800), children=[pill])
        items, _rects = navigator(root)._items()
        self.assertEqual(items, [pill])

    def test_nav_skip_removes_a_widget_and_everything_under_it(self):
        # The status row reacts to <Button-1> because it drags the activity
        # log open, which does not make it a control.
        status = Widget("CTkFrame", (0, 600, 1000, 30), clickable=True,
                        children=[Widget("CTkLabel", (0, 600, 400, 20),
                                         clickable=True)])
        status._nav_skip = True
        play = button((880, 690, 110, 52))
        root = Widget("CTk", (0, 0, 1000, 800), children=[status, play])
        items, _rects = navigator(root)._items()
        self.assertEqual(items, [play])

    def test_disabled_and_unmapped_controls_are_left_out(self):
        play = button((880, 690, 110, 52))
        root = Widget("CTk", (0, 0, 1000, 800), children=[
            play,
            button((700, 690, 80, 52), state="disabled"),
            button((600, 690, 80, 52), mapped=False),
        ])
        items, _rects = navigator(root)._items()
        self.assertEqual(items, [play])

    def test_a_scrollbar_is_not_a_stop(self):
        play = button((880, 690, 110, 52))
        bar = Widget("CTkScrollbar", (990, 100, 10, 400), clickable=True)
        root = Widget("CTk", (0, 0, 1000, 800), children=[bar, play])
        items, _rects = navigator(root)._items()
        self.assertEqual(items, [play])

    def test_tab_headers_hidden_from_winfo_children_are_still_found(self):
        general = button((430, 272, 72, 27))
        advanced = button((502, 272, 87, 27))
        tabs = Widget("CTkTabview", (400, 260, 400, 300))
        tabs._segmented_button = Widget(
            "CTkSegmentedButton", (430, 272, 160, 27),
            children=[general, advanced])
        tabs._segmented_button.master = tabs
        root = Widget("CTk", (0, 0, 1000, 800), children=[tabs])
        items, _rects = navigator(root)._items()
        self.assertEqual(items, [general, advanced])

    def test_controls_scrolled_out_of_a_panel_are_out_of_reach(self):
        # A scrollable frame keeps its off-screen rows mapped; the ring must
        # not jump to a switch nobody can see.
        visible = Widget("CTkSwitch", (120, 300, 200, 24))
        below = Widget("CTkSwitch", (120, 900, 200, 24))
        panel = Widget("CTkScrollableFrame", (100, 200, 800, 400),
                       children=[visible, below])
        panel._parent_canvas = Widget("Canvas", (100, 200, 800, 400))
        root = Widget("CTk", (0, 0, 1000, 800), children=[panel])
        items, _rects = navigator(root)._items()
        self.assertEqual(items, [visible])


class MovementTests(unittest.TestCase):
    def _settings(self):
        """A panel of switches with a button ranged along its right edge."""
        self.rows = [Widget("CTkSwitch", (120, 260 + index * 40, 200, 24))
                     for index in range(4)]
        self.unlink = button((800, 300, 88, 28))
        self.panel = Widget("CTkScrollableFrame", (100, 240, 800, 300),
                            children=self.rows + [self.unlink])
        self.canvas = Widget("Canvas", (100, 240, 800, 300),
                             scrollregion="0 0 800 1200")
        self.panel._parent_canvas = self.canvas
        self.play = button((880, 690, 110, 52))
        return Widget("CTk", (0, 0, 1000, 800),
                      children=[self.panel, self.play])

    def test_a_panel_is_walked_in_reading_order_not_by_column(self):
        root = self._settings()
        nav = navigator(root, primary_item=lambda: self.play)
        nav._show_ring(self.rows[0])
        nav.dispatch("down")
        self.assertIs(nav._current, self.rows[1])
        nav.dispatch("down")            # the button ranged along that same row
        self.assertIs(nav._current, self.unlink)
        nav.dispatch("down")
        self.assertIs(nav._current, self.rows[2])
        nav.dispatch("up")
        self.assertIs(nav._current, self.unlink)

    def test_leaving_the_panel_falls_back_to_the_nearest_control(self):
        root = self._settings()
        nav = navigator(root, primary_item=lambda: self.play)
        self.canvas.view = [0.0, 1.0]              # nothing left to scroll
        nav._show_ring(self.rows[-1])
        nav.dispatch("down")
        self.assertIs(nav._current, self.play)

    def test_a_press_with_more_list_below_scrolls_instead_of_jumping(self):
        root = self._settings()
        nav = navigator(root, primary_item=lambda: self.play)
        self.canvas.view = [0.0, 0.4]              # more list below the fold
        nav._show_ring(self.rows[-1])
        nav.dispatch("down")
        self.assertIs(nav._current, self.rows[-1])  # stayed put …
        self.assertGreater(self.canvas.view[0], 0.0)   # … and scrolled

    def test_the_ring_starts_on_the_primary_control(self):
        root = self._settings()
        nav = navigator(root, primary_item=lambda: self.play)
        nav.dispatch("down")
        self.assertIs(nav._current, self.play)
        self.assertTrue(nav._shown)

    def test_the_mouse_hides_the_ring_and_a_press_brings_it_back(self):
        root = self._settings()
        nav = navigator(root, primary_item=lambda: self.play)
        nav._show_ring(self.rows[1])
        nav._on_motion()
        self.assertFalse(nav._shown)
        nav.dispatch("down")                       # resumes, does not move
        self.assertTrue(nav._shown)
        self.assertIs(nav._current, self.rows[1])

    def test_the_ring_puts_back_what_it_borrowed(self):
        root = self._settings()
        nav = navigator(root)
        before = dict(self.play.options)
        nav._show_ring(self.play)
        self.assertNotEqual(self.play.options, before)
        nav._hide_ring()
        self.assertEqual(self.play.options, before)


class InputGateTests(unittest.TestCase):
    """What reaches the window, and what the pump drops on the floor."""

    def setUp(self):
        self.play = button((880, 690, 110, 52))
        self.root = Widget("CTk", (0, 0, 1000, 800), children=[self.play])
        self.root.viewable = True
        self.root.winfo_viewable = lambda: self.root.viewable

    def test_input_is_ignored_while_the_game_is_running(self):
        running = {"game": True}
        nav = navigator(self.root, primary_item=lambda: self.play,
                        accepts_input=lambda: not running["game"])
        self.assertFalse(nav._ready())
        running["game"] = False
        self.assertTrue(nav._ready())

    def test_input_is_ignored_while_the_window_has_stepped_aside(self):
        nav = navigator(self.root, primary_item=lambda: self.play)
        self.root.viewable = False
        self.assertFalse(nav._ready())
        self.root.viewable = True
        self.assertTrue(nav._ready())

    def test_the_pump_only_delivers_when_the_window_is_ready(self):
        blocked = {"value": True}
        nav = navigator(self.root, primary_item=lambda: self.play,
                        accepts_input=lambda: not blocked["value"])
        nav._queue.put("accept")
        nav._pump()
        self.assertEqual(self.play.invoked, 0)
        blocked["value"] = False
        nav._queue.put("down")
        nav._queue.put("accept")
        nav._pump()
        self.assertEqual(self.play.invoked, 1)

    def test_turning_navigation_off_takes_the_ring_away(self):
        nav = navigator(self.root, primary_item=lambda: self.play)
        nav._show_ring(self.play)
        nav.set_enabled(False)
        self.assertFalse(nav._shown)
        nav._queue.put("accept")
        nav._pump()
        self.assertEqual(self.play.invoked, 0)


class ActionTests(unittest.TestCase):
    def test_accept_invokes_a_button_and_toggles_a_switch(self):
        play = button((880, 690, 110, 52))
        switch = Widget("CTkSwitch", (120, 300, 200, 24))
        root = Widget("CTk", (0, 0, 1000, 800), children=[play, switch])
        nav = navigator(root)
        nav._show_ring(play)
        nav.dispatch("accept")
        self.assertEqual(play.invoked, 1)
        nav._show_ring(switch)
        nav.dispatch("accept")
        self.assertEqual(switch.toggled, 1)

    def test_accept_on_a_text_field_hands_it_the_keyboard(self):
        entry = Widget("CTkEntry", (120, 300, 200, 24))
        root = Widget("CTk", (0, 0, 1000, 800), children=[entry])
        nav = navigator(root)
        nav._show_ring(entry)
        nav.dispatch("accept")
        self.assertEqual(entry.focused, 1)

    def test_accept_clicks_a_widget_that_is_only_clickable(self):
        pill = Widget("CTkFrame", (88, 690, 220, 52), clickable=True)
        root = Widget("CTk", (0, 0, 1000, 800), children=[pill])
        nav = navigator(root)
        nav._show_ring(pill)
        nav.dispatch("accept")
        self.assertEqual(pill._canvas.events,
                         ["<Button-1>", "<ButtonRelease-1>"])

    def test_accept_does_nothing_while_the_ring_is_hidden(self):
        play = button((880, 690, 110, 52))
        root = Widget("CTk", (0, 0, 1000, 800), children=[play])
        nav = navigator(root, primary_item=lambda: play)
        nav.dispatch("accept")
        self.assertEqual(play.invoked, 0)
        self.assertTrue(nav._shown)
        nav.dispatch("accept")
        self.assertEqual(play.invoked, 1)

    def test_start_plays_and_back_leaves_the_view(self):
        called = []
        play = button((880, 690, 110, 52))
        root = Widget("CTk", (0, 0, 1000, 800), children=[play])
        nav = navigator(root, on_start=lambda: called.append("start"),
                        on_back=lambda: called.append("back"))
        nav.dispatch("start")
        nav.dispatch("back")
        self.assertEqual(called, ["start", "back"])

    def test_a_dropdown_scope_confines_the_ring_and_b_closes_it(self):
        closed = []
        play = button((880, 690, 110, 52))
        rows = [button((100, 400 + index * 32, 200, 30)) for index in range(3)]
        menu = Widget("CTkFrame", (90, 390, 220, 120), children=rows)
        root = Widget("CTk", (0, 0, 1000, 800), children=[menu, play])
        nav = navigator(root, primary_item=lambda: play,
                        on_back=lambda: closed.append("view"))
        nav.dispatch("down")
        self.assertIs(nav._current, play)
        nav.push_scope(menu, on_back=lambda: closed.append("menu"))
        items, _rects = nav._items()
        self.assertEqual(items, rows)
        self.assertIs(nav._current, rows[0])
        nav.dispatch("back")
        self.assertEqual(closed, ["menu"])
        nav.pop_scope(menu)
        self.assertIs(nav._current, play)

    def test_a_scope_that_disappears_is_dropped(self):
        play = button((880, 690, 110, 52))
        menu = Widget("CTkFrame", (90, 390, 220, 120), mapped=False)
        root = Widget("CTk", (0, 0, 1000, 800), children=[play])
        nav = navigator(root, primary_item=lambda: play)
        nav.push_scope(menu)
        items, _rects = nav._items()
        self.assertEqual(items, [play])
        self.assertEqual(nav._scopes, [])

    def test_the_shoulder_buttons_change_tab(self):
        general = button((430, 272, 72, 27))
        tabs = Widget("CTkTabview", (400, 260, 400, 300))
        tabs._segmented_button = Widget("CTkSegmentedButton",
                                        (430, 272, 160, 27),
                                        children=[general])
        tabs._segmented_button.master = tabs
        tabs._name_list = ["General", "Advanced", "Tools"]
        tabs.current = "General"
        tabs.get = lambda: tabs.current
        tabs.set = lambda name: setattr(tabs, "current", name)
        root = Widget("CTk", (0, 0, 1000, 800), children=[tabs])
        nav = navigator(root)
        nav.dispatch("next_tab")
        self.assertEqual(tabs.current, "Advanced")
        nav.dispatch("prev_tab")
        self.assertEqual(tabs.current, "General")
        nav.dispatch("prev_tab")
        self.assertEqual(tabs.current, "Tools")     # wraps round

    def test_the_scroll_stick_moves_the_view_the_ring_is_in(self):
        row = Widget("CTkSwitch", (120, 300, 200, 24))
        panel = Widget("CTkScrollableFrame", (100, 240, 800, 300),
                       children=[row])
        canvas = Widget("Canvas", (100, 240, 800, 300),
                        scrollregion="0 0 800 1200")
        panel._parent_canvas = canvas
        root = Widget("CTk", (0, 0, 1000, 800), children=[panel])
        nav = navigator(root)
        nav._show_ring(row)
        canvas.view = [0.5, 0.8]
        nav.dispatch("scroll_down")
        self.assertGreater(canvas.view[0], 0.5)
        canvas.view = [0.5, 0.8]
        nav.dispatch("scroll_up")
        self.assertLess(canvas.view[0], 0.5)


if __name__ == "__main__":
    unittest.main()

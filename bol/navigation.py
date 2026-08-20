"""bol.navigation — drive the launcher window with a game controller.

`bol.gamepad` turns a controller into logical actions; this module turns those
actions into a moving focus ring over the customtkinter window, so the whole
launcher — PLAY, the version picker, sign-in, every Settings switch — can be
operated without a mouse.  That is the difference between the launcher being a
five-second stop and being a dead end on a Steam Deck or any Game Mode session,
where the launcher deliberately still opens before the game.

The ring is discovered, not declared.  On every press the widget tree of the
active scope is walked and each visible, enabled control becomes a ring item;
nothing in `bol.gui` has to register its buttons, and a settings card added
later is navigable the day it is written.  Two kinds of widget qualify:

* the customtkinter controls (`CTkButton`, `CTkSwitch`, `CTkEntry`, …), and
* anything carrying a ``<Button-1>`` binding — the version pill, the profile
  chip and the What's New label are frames and labels made clickable by hand,
  and to a controller user they are buttons like any other.

Movement is spatial rather than a flat tab order: `choose_neighbour` picks the
control that actually lies in the direction pressed, preferring one whose box
overlaps the current one on the other axis.  Pressing right from PLAY reaches
Settings because it sits to the right of PLAY, not because of the order the two
were created in.

Two deliberate restraints:

* the ring stays hidden until a controller is actually used, and disappears
  again on the first mouse movement, so a keyboard-and-mouse user never sees
  a focus rectangle the toolkit did not draw;
* actions are dropped while the window is withdrawn or Minecraft is running,
  because the controller keeps reporting to whoever is listening and the
  launcher is still alive behind the game.

That second test deliberately does not ask whether the launcher holds the X
input focus, which is the obvious way to write it. A window manager that
refuses focus to a window it did not see the user open — focus-stealing
prevention, focus-follows-mouse with the pointer elsewhere, a kiosk session —
would then leave the controller doing nothing at all, and the user has no
mouse with which to fix it. Being on screen and not behind the game is the
condition that actually matters.
"""
# SPDX-License-Identifier: MIT

import queue

from .gamepad import GamepadReader

# Controls the ring can land on. Everything else in the tree is either a
# container to descend into or decoration to skip.
INTERACTIVE_CLASSES = frozenset({
    "CTkButton", "CTkSwitch", "CTkCheckBox", "CTkEntry", "CTkOptionMenu",
    "CTkComboBox", "CTkRadioButton", "CTkSlider",
})

# Containers whose children are part of the same screen. `CTkSegmentedButton`
# and `CTkTabview` are here so their individual tab buttons become ring items.
# A bare `Canvas` is on the list because that is how customtkinter builds a
# scrollable frame: the content lives in a frame drawn inside a canvas, and
# skipping canvases would hide every control in Settings.
CONTAINER_CLASSES = frozenset({
    "CTk", "CTkToplevel", "Tk", "Toplevel", "Frame", "LabelFrame",
    "CTkFrame", "CTkScrollableFrame", "CTkTabview", "CTkSegmentedButton",
    "Canvas", "CTkCanvas",
})

# Widgets that are chrome rather than controls. A scrollbar carries the
# ``<Button-1>`` binding it drags itself with, which would otherwise read as a
# clickable widget; the ring reaches what it scrolls to anyway.
SKIP_CLASSES = frozenset({"CTkScrollbar", "Scrollbar"})

# Widgets narrower or shorter than this are placeholders mid-layout.
_MIN_SIZE = 4

# How far past the edge of a view a control may sit and still be a ring item.
# A scrollable frame leaves its off-screen rows mapped, so without a bound the
# ring would jump from the dock into a Settings row nobody can see; with a
# margin roughly one row deep, the control just past the fold stays reachable,
# which is what makes walking down a long list scroll it.
_VIEWPORT_MARGIN = 48

# How far a candidate must lie in the pressed direction to count as a move.
_MIN_STEP = 6

# Cost multiplier for sideways drift, when the two boxes overlap on the other
# axis and when they do not. Overlapping neighbours win comfortably without
# a far-away one beating a close, slightly offset control.
_ALIGNED_DRIFT = 0.25
_UNALIGNED_DRIFT = 3.0

# Pixels the scroll stick moves a view per repeat.
_SCROLL_STEP = 26

# Queue marker for "the set of connected controllers changed".
_DEVICES = "devices"

_HORIZONTAL = ("left", "right")


def _centre(rect):
    x, y, width, height = rect
    return x + width / 2.0, y + height / 2.0


def _overlaps(start_a, size_a, start_b, size_b):
    return (start_a < start_b + size_b) and (start_b < start_a + size_a)


def within(rect, view, margin=0):
    """Whether a widget box shows up inside a view box, give or take a margin."""
    return (_overlaps(rect[0], rect[2], view[0] - margin, view[2] + 2 * margin)
            and _overlaps(rect[1], rect[3],
                          view[1] - margin, view[3] + 2 * margin))


def choose_neighbour(rects, current, direction, wrap=True):
    """Index of the item a d-pad press moves to, or None when there is none.

    `rects` are (x, y, width, height) boxes in a common coordinate space and
    `current` indexes the focused one. With `wrap`, a press with nothing ahead
    of it comes back around from the far side, so every control stays reachable
    with the d-pad alone; the caller turns wrapping off first to find out
    whether it should scroll rather than jump.
    """
    if not rects:
        return None
    if current is None or not 0 <= current < len(rects):
        return 0
    horizontal = direction in _HORIZONTAL
    forward = direction in ("right", "down")
    current_rect = rects[current]
    current_x, current_y = _centre(current_rect)

    def measure(rect):
        other_x, other_y = _centre(rect)
        if horizontal:
            step = other_x - current_x
            drift = abs(other_y - current_y)
            aligned = _overlaps(current_rect[1], current_rect[3],
                                rect[1], rect[3])
        else:
            step = other_y - current_y
            drift = abs(other_x - current_x)
            aligned = _overlaps(current_rect[0], current_rect[2],
                                rect[0], rect[2])
        if not forward:
            step = -step
        return step, drift, aligned

    def weigh(step, drift, aligned):
        return step + drift * (_ALIGNED_DRIFT if aligned else _UNALIGNED_DRIFT)

    best = best_cost = None
    behind = []
    for index, rect in enumerate(rects):
        if index == current:
            continue
        step, drift, aligned = measure(rect)
        if step < _MIN_STEP:
            behind.append((index, rect, drift, aligned))
            continue
        cost = weigh(step, drift, aligned)
        if best_cost is None or cost < best_cost:
            best, best_cost = index, cost
    if best is not None or not wrap:
        return best

    # Nothing ahead: wrap to the item furthest back, nearest in line with the
    # current one.
    axis = 0 if horizontal else 1
    size_axis = axis + 2
    edge = None
    for _index, rect, _drift, _aligned in behind:
        far_side = rect[axis] + (0 if forward else rect[size_axis])
        edge = far_side if edge is None else (
            min(edge, far_side) if forward else max(edge, far_side))
    if edge is None:
        return None
    for index, rect, drift, aligned in behind:
        far_side = rect[axis] + (0 if forward else rect[size_axis])
        distance = abs(far_side - edge)
        cost = weigh(distance, drift, aligned)
        if best_cost is None or cost < best_cost:
            best, best_cost = index, cost
    return best


def reading_order(items, rects):
    """`items` sorted the way the eye reads them: down, then across."""
    return [item for _rect, item in
            sorted(zip(rects, items), key=lambda pair: (pair[0][1], pair[0][0]))]


class ControllerNav:
    """Focus ring over a customtkinter window, driven by a controller.

    The GUI owns the widgets; this owns which one is lit and what a button
    press does to it. Callbacks let `bol.gui` keep the decisions that are not
    generic: `on_back` for the in-window views (Settings, What's New) that
    Escape would close, `on_start` for the Start button, and `on_devices` to
    show or hide the on-screen button legend.
    """

    POLL_MS = 32                  # how often the Tk loop drains the reader
    RING_WIDTH = 3

    def __init__(self, root, ctk, tk, accent,
                 on_back=None, on_start=None, on_devices=None,
                 primary_item=None, accepts_input=None, reader_factory=None):
        self._root = root
        self._ctk = ctk
        self._tk = tk
        self._accent = accent
        self._on_back = on_back
        self._on_start = on_start
        self._on_devices = on_devices
        self._primary_item = primary_item
        self._accepts_input = accepts_input
        self._reader_factory = reader_factory or GamepadReader
        self._reader = None
        self._queue = queue.Queue()
        self._pump_job = None
        self._current = None          # widget the ring is on, shown or not
        self._shown = False
        self._restore = None          # (widget, {option: value}) to undo
        self._scopes = []             # [(widget, on_back, previous item)]
        self._enabled = True
        self._motion_bind = None

    # -- lifecycle --------------------------------------------------------
    def start(self):
        """Start watching for a controller. False when none can be read."""
        if self._reader is not None:
            return True
        reader = self._reader_factory(self._queue.put, self._devices_changed)
        if not reader.start():
            return False
        self._reader = reader
        self._motion_bind = self._root.bind_all(
            "<Motion>", self._on_motion, add="+")
        self._schedule_pump()
        return True

    def stop(self):
        if self._pump_job is not None:
            try:
                self._root.after_cancel(self._pump_job)
            except Exception:
                pass
            self._pump_job = None
        if self._motion_bind is not None:
            try:
                self._root.unbind_all("<Motion>")
            except Exception:
                pass
            self._motion_bind = None
        self._hide_ring()
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.stop()

    def set_enabled(self, enabled):
        """Turn navigation off without unplugging the reader."""
        self._enabled = bool(enabled)
        if not self._enabled:
            self._hide_ring()

    @property
    def device_names(self):
        return self._reader.device_names if self._reader is not None else ()

    # -- scopes -----------------------------------------------------------
    def push_scope(self, widget, on_back=None):
        """Confine the ring to `widget` — a dropdown or menu placed over the
        window. These are plain frames, not toplevels, so nothing else can tell
        that the rest of the window is out of play."""
        self._scopes.append((widget, on_back, self._current))
        self._current = None
        if self._shown:
            self._move_to_entry()

    def pop_scope(self, widget=None):
        if not self._scopes:
            return
        if widget is not None and self._scopes[-1][0] is not widget:
            self._scopes = [scope for scope in self._scopes
                            if scope[0] is not widget]
            return
        _widget, _on_back, previous = self._scopes.pop()
        self._current = previous
        if self._shown:
            self._show_ring(self._current)

    # -- widget discovery -------------------------------------------------
    def _class_of(self, widget):
        return type(widget).__name__

    def _is_visible(self, widget):
        try:
            return (widget.winfo_ismapped()
                    and widget.winfo_width() > _MIN_SIZE
                    and widget.winfo_height() > _MIN_SIZE)
        except Exception:
            return False

    def _is_enabled(self, widget):
        try:
            return str(widget.cget("state")) != "disabled"
        except Exception:
            return True

    def _children(self, widget):
        """`winfo_children`, plus the parts customtkinter hides from it.

        `CTkTabview.winfo_children` drops the segmented button holding the tab
        headers, which are exactly the controls a shoulder button or the ring
        has to reach.
        """
        try:
            children = list(widget.winfo_children())
        except Exception:
            return []
        tabs = getattr(widget, "_segmented_button", None)
        if tabs is not None and tabs not in children:
            children.insert(0, tabs)
        return children

    def _click_target(self, widget):
        """The internal child carrying a ``<Button-1>`` binding, if any.

        customtkinter forwards `bind()` to the canvas and text label it draws
        itself with, and `CTkFrame.winfo_children` then hides that canvas, so a
        clickable frame has its binding in a place neither `bind()` nor the
        child list will admit to. `tkinter.Misc.bind` is called unbound to
        bypass the override, whose query form returns nothing.
        """
        candidates = [widget]
        canvas = getattr(widget, "_canvas", None)
        if canvas is not None:
            candidates.append(canvas)
        try:
            candidates += [child for child in widget.winfo_children()
                           if not isinstance(child, self._ctk.CTkBaseClass)]
        except Exception:
            pass
        for candidate in candidates:
            try:
                bound = self._tk.Misc.bind(candidate)
            except Exception:
                continue
            if bound and "<Button-1>" in bound:
                return candidate
        return None

    def _collect(self, parent, found):
        for child in self._children(parent):
            # `_nav_skip` is how bol.gui excludes a widget that reacts to a
            # click without being a control — the activity-log drag handle,
            # the version picker's resize grip.
            if getattr(child, "_nav_skip", False):
                continue
            if not self._is_visible(child):
                continue
            name = self._class_of(child)
            if name in SKIP_CLASSES:
                continue
            if name in INTERACTIVE_CLASSES:
                if self._is_enabled(child):
                    found.append(child)
                continue
            if name in CONTAINER_CLASSES:
                # A container that is itself clickable is one control, not a
                # box of them: the version pill holds two labels and an arrow.
                if self._click_target(child) is not None:
                    found.append(child)
                else:
                    self._collect(child, found)
                continue
            if self._click_target(child) is not None:
                found.append(child)
        return found

    def _scope(self):
        """The widget subtree the ring may move in right now."""
        while self._scopes:
            widget = self._scopes[-1][0]
            try:
                if widget.winfo_exists() and widget.winfo_ismapped():
                    return widget
            except Exception:
                pass
            self._scopes.pop()          # closed without telling us
        return self._active_toplevel()

    def _active_toplevel(self):
        """The dialog on top, or the main window."""
        try:
            grabbed = self._root.grab_current()
        except Exception:
            grabbed = None
        if grabbed is not None:
            try:
                return grabbed.winfo_toplevel()
            except Exception:
                pass
        newest = None
        try:
            for child in self._root.winfo_children():
                if (isinstance(child, self._tk.Toplevel)
                        and child.winfo_ismapped()):
                    newest = child
        except Exception:
            pass
        return newest if newest is not None else self._root

    def _box(self, widget):
        try:
            return (widget.winfo_rootx(), widget.winfo_rooty(),
                    widget.winfo_width(), widget.winfo_height())
        except Exception:
            return None

    def _on_screen(self, widget, rect):
        """Whether a control is inside the part of the window on display."""
        for view in (self._scroll_canvas(widget), widget.winfo_toplevel()):
            if view is None:
                continue
            box = self._box(view)
            if box is not None and not within(rect, box, _VIEWPORT_MARGIN):
                return False
        return True

    def _items(self, scope=None):
        if scope is None:
            scope = self._scope()
        try:
            items = self._collect(scope, [])
        except Exception:
            return [], []
        rects = []
        keep = []
        for item in items:
            rect = self._box(item)
            if rect is None or not self._on_screen(item, rect):
                continue
            keep.append(item)
            rects.append(rect)
        return keep, rects

    # -- the ring itself --------------------------------------------------
    def _ring_options(self, widget):
        """How to light this widget up: a border where the class draws one,
        a filled background where it does not (plain labels)."""
        options = {"border_width": self.RING_WIDTH, "border_color": self._accent}
        if self._class_of(widget) == "CTkSwitch":
            options["text_color"] = self._accent
        return options

    def _apply(self, widget, options):
        """Set options one at a time, keeping the previous values."""
        previous = {}
        for option, value in options.items():
            try:
                previous[option] = widget.cget(option)
                widget.configure(**{option: value})
            except Exception:
                previous.pop(option, None)
        return previous

    def _hide_ring(self):
        self._shown = False
        if self._restore is None:
            return
        widget, previous = self._restore
        self._restore = None
        try:
            if widget.winfo_exists():
                widget.configure(**previous)
        except Exception:
            pass

    def _show_ring(self, widget):
        self._hide_ring()
        if widget is None:
            return
        previous = self._apply(widget, self._ring_options(widget))
        if not previous:
            # A plain label draws no border at all; tint its text instead, the
            # same thing the clickable ones already do under the pointer.
            previous = self._apply(widget, {"text_color": self._accent})
        self._restore = (widget, previous)
        self._shown = True
        self._current = widget
        self._reveal(widget)

    # -- scrolling --------------------------------------------------------
    def _scroll_canvas(self, widget):
        """The scrollable canvas `widget` sits in, if any."""
        node = widget
        for _depth in range(24):
            if node is None:
                return None
            canvas = getattr(node, "_parent_canvas", None)
            if canvas is not None:
                return canvas
            node = getattr(node, "master", None)
        return None

    def _scroll_by(self, view, pixels):
        """Scroll a view by a pixel amount, whatever kind of view it is.

        A canvas measures its content in pixels through ``scrollregion``, so
        the step lands exactly; a text widget has no such measure and falls
        back to its own line-based scrolling.
        """
        try:
            region = str(view.cget("scrollregion")).split()
        except Exception:
            region = []
        try:
            if len(region) == 4:
                total = float(region[3]) - float(region[1])
                if total > 0:
                    view.yview_moveto(max(0.0, min(
                        1.0, view.yview()[0] + pixels / total)))
                    return
            view.yview_scroll(-1 if pixels < 0 else 1, "units")
        except Exception:
            pass

    def _reveal(self, widget):
        """Scroll `widget` into view when it sits in a scrollable frame."""
        canvas = self._scroll_canvas(widget)
        if canvas is None:
            return
        try:
            top = widget.winfo_rooty() - canvas.winfo_rooty()
            bottom = top + widget.winfo_height()
            height = canvas.winfo_height()
        except Exception:
            return
        margin = 14
        if top < margin:
            self._scroll_by(canvas, top - margin)
        elif bottom > height - margin:
            self._scroll_by(canvas, bottom - height + margin)

    def _scroll(self, direction):
        canvas = self._scroll_canvas(self._current) if self._current else None
        if canvas is None:
            canvas = self._largest_scrollable(self._scope())
        if canvas is None:
            return
        self._scroll_by(canvas, -_SCROLL_STEP if direction == "scroll_up"
                        else _SCROLL_STEP)

    def _largest_scrollable(self, scope):
        """The biggest scrollable view on screen — what the stick scrolls when
        the ring is not sitting inside one (the activity log, the changelog)."""
        found = self._scrollables(scope, [])
        if not found:
            return None
        return max(found, key=lambda pair: pair[0])[1]

    def _scrollables(self, parent, found):
        try:
            children = parent.winfo_children()
        except Exception:
            return found
        for child in children:
            view = getattr(child, "_parent_canvas", None)
            if view is None and self._class_of(child) in ("Text", "CTkTextbox"):
                view = child
            if view is not None and self._is_visible(child):
                try:
                    found.append(
                        (child.winfo_width() * child.winfo_height(), view))
                except Exception:
                    pass
            self._scrollables(child, found)
        return found

    # -- actions ----------------------------------------------------------
    def _move_to_entry(self):
        """Light the ring up where it makes sense to start."""
        items, rects = self._items()
        if not items:
            return
        preferred = None
        if not self._scopes and self._primary_item is not None:
            try:
                preferred = self._primary_item()
            except Exception:
                preferred = None
        if preferred is not None and preferred in items:
            self._show_ring(preferred)
            return
        self._show_ring(reading_order(items, rects)[0])

    def _enter(self, container, retry=True):
        """Put the ring on the first control inside a container.

        A tab that has just been raised may not have been through the geometry
        manager yet, so nothing in it looks visible; one retry a frame later
        catches that instead of dropping the ring back to PLAY.
        """
        try:
            self._root.update_idletasks()
        except Exception:
            pass
        items, rects = self._items(container)
        if items:
            self._show_ring(reading_order(items, rects)[0])
        elif retry:
            self._root.after(80, lambda: self._enter(container, retry=False))

    def _list_step(self, items, rects, current, direction):
        """The next control down (or up) a scrollable panel, in reading order.

        Purely spatial movement is right for a row of buttons but wrong for
        Settings: straight down from a switch on the left is the next switch on
        the left, and the buttons ranged along the right of the same panel
        would only ever be reachable by guessing which row they share. Inside a
        scrollable panel the ring therefore walks the list the way the eye
        does, and left/right stays spatial for the groups within a row.
        """
        canvas = self._scroll_canvas(items[current])
        if canvas is None:
            return None
        group = [index for index in range(len(items))
                 if self._scroll_canvas(items[index]) is canvas]
        if current not in group or len(group) < 2:
            return None
        group.sort(key=lambda index: (rects[index][1], rects[index][0]))
        position = group.index(current) + (1 if direction == "down" else -1)
        if 0 <= position < len(group):
            return group[position]
        return None

    def _move(self, direction):
        items, rects = self._items()
        if not items:
            return
        if self._current not in items:
            self._move_to_entry()
            return
        index = None
        vertical = direction in ("up", "down")
        if vertical:
            index = self._list_step(
                items, rects, items.index(self._current), direction)
            if index is None:
                # The panel has nothing further in that direction on screen.
                # Scrolling comes before any spatial fallback, or a press at
                # the bottom of Settings would leap to the dock underneath
                # instead of revealing the rest of the list.
                items, rects, scrolled = self._scroll_towards(
                    direction, items, rects)
                if scrolled:
                    if self._current not in items:
                        return
                    index = self._list_step(
                        items, rects, items.index(self._current), direction)
                    if index is None:
                        return          # the press spent itself scrolling
        if index is None:
            current = items.index(self._current)
            index = choose_neighbour(rects, current, direction, wrap=False)
            if index is None:
                index = choose_neighbour(rects, current, direction)
        if index is not None:
            self._show_ring(items[index])

    def _scroll_towards(self, direction, items, rects):
        """Scroll the panel the ring sits in. Returns refreshed items, boxes
        and whether anything actually moved."""
        canvas = self._scroll_canvas(self._current)
        if canvas is None:
            return items, rects, False
        try:
            first, last = canvas.yview()[:2]
        except Exception:
            return items, rects, False
        if (direction == "down" and last >= 0.999) or (
                direction == "up" and first <= 0.001):
            return items, rects, False
        self._scroll_by(canvas, _SCROLL_STEP * (-3 if direction == "up" else 3))
        try:
            self._root.update_idletasks()
        except Exception:
            pass
        items, rects = self._items()
        return items, rects, True

    def _activate(self):
        widget = self._current
        if widget is None or not self._shown:
            self._resume()          # never activate what is not lit up
            return
        try:
            if not widget.winfo_exists():
                self._current = None
                return
        except Exception:
            return
        name = self._class_of(widget)
        try:
            if name == "CTkButton":
                widget.invoke()
                return
            if name in ("CTkSwitch", "CTkCheckBox"):
                widget.toggle()
                return
            if name in ("CTkEntry", "CTkTextbox"):
                widget.focus_set()
                return
        except Exception:
            return
        target = self._click_target(widget)
        if target is None:
            return
        try:
            target.event_generate("<Button-1>", x=1, y=1)
            target.event_generate("<ButtonRelease-1>", x=1, y=1)
        except Exception:
            pass

    def _back(self):
        if self._scopes:
            _widget, on_back, _previous = self._scopes[-1]
            if on_back is not None:
                try:
                    on_back()
                except Exception:
                    pass
                return
        toplevel = self._active_toplevel()
        if toplevel is not self._root:
            try:
                toplevel.event_generate("<Escape>")
                return
            except Exception:
                pass
        if self._on_back is not None:
            try:
                self._on_back()
            except Exception:
                pass

    def _tabview_for(self, widget):
        node = widget
        for _depth in range(24):
            if node is None:
                break
            if self._class_of(node) == "CTkTabview":
                return node
            node = getattr(node, "master", None)
        return self._first_tabview(self._scope())

    def _first_tabview(self, parent):
        try:
            children = parent.winfo_children()
        except Exception:
            return None
        for child in children:
            if self._class_of(child) == "CTkTabview" and self._is_visible(child):
                return child
            found = self._first_tabview(child)
            if found is not None:
                return found
        return None

    def _switch_tab(self, step):
        tabview = self._tabview_for(self._current)
        if tabview is None:
            return
        names = list(getattr(tabview, "_name_list", None)
                     or getattr(tabview, "_tab_dict", {}).keys())
        if len(names) < 2:
            return
        try:
            index = names.index(tabview.get())
        except Exception:
            index = 0
        tabview.set(names[(index + step) % len(names)])
        # The old tab's controls are gone; land on the new tab's first one
        # rather than falling back to wherever the ring normally starts.
        self._current = None
        if self._shown:
            self._root.after_idle(lambda: self._enter(tabview))

    # -- event plumbing ---------------------------------------------------
    def _devices_changed(self, names):
        """Called from the reader thread when a controller comes or goes.

        It goes through the same queue as the buttons rather than straight to
        `root.after`: Tkinter belongs to the thread running the main loop, and
        calling into it from here is the one way this module could take the
        whole window down.
        """
        self._queue.put((_DEVICES, tuple(names)))

    def _on_motion(self, _event=None):
        """A mouse user is driving: take the ring away again."""
        if self._shown:
            self._hide_ring()

    def _ready(self):
        """Whether the launcher should be acting on controller input at all.

        False while the window is withdrawn — which is what it does in Game
        Mode when it steps aside for Minecraft — and while the owner says the
        game is running.
        """
        try:
            if not self._root.winfo_viewable():
                return False
        except Exception:
            pass
        if self._accepts_input is None:
            return True
        try:
            return bool(self._accepts_input())
        except Exception:
            return True

    def _schedule_pump(self):
        try:
            self._pump_job = self._root.after(self.POLL_MS, self._pump)
        except Exception:
            self._pump_job = None

    def _pump(self):
        actions = []
        while True:
            try:
                actions.append(self._queue.get_nowait())
            except queue.Empty:
                break
        ready = self._enabled and self._ready()
        for action in actions:
            try:
                if isinstance(action, tuple):
                    # A controller was plugged in or unplugged. Reported even
                    # when input is being ignored, so the on-screen legend
                    # still tells the truth.
                    if action[0] == _DEVICES and self._on_devices is not None:
                        self._on_devices(action[1])
                elif ready:
                    self.dispatch(action)
            except Exception:
                pass
        self._schedule_pump()

    def _resume(self):
        """Bring the ring back where it was, or start it somewhere sensible.

        The first press after the ring is hidden — at startup, or once a mouse
        movement has taken it away — only makes it visible again. It does not
        move and it does not activate, so picking the mouse up and putting it
        down again never costs the user their place.
        """
        items, _rects = self._items()
        if self._current is not None and self._current in items:
            self._show_ring(self._current)
        else:
            self._move_to_entry()

    def dispatch(self, action):
        """Apply one logical controller action to the window."""
        if action in ("up", "down", "left", "right"):
            if self._shown:
                self._move(action)
            else:
                self._resume()
        elif action == "accept":
            self._activate()
        elif action == "back":
            self._back()
        elif action == "start":
            if not self._scopes and self._on_start is not None:
                self._on_start()
        elif action == "prev_tab":
            self._switch_tab(-1)
        elif action == "next_tab":
            self._switch_tab(1)
        elif action in ("scroll_up", "scroll_down"):
            self._scroll(action)

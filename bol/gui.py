"""bol.gui — the desktop GUI (PySide6)."""
# SPDX-License-Identifier: MIT

from __future__ import annotations

import html
import inspect
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import (
    QEvent, QObject, QPoint, QPointF, Qt, QThread, QTimer, Signal, Slot,
)
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractButton, QApplication, QButtonGroup, QDialog, QFileDialog, QFrame,
    QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QStackedWidget, QTabWidget, QTextBrowser, QTextEdit,
    QToolButton, QVBoxLayout, QWidget,
)

from .auth import NativeAuth, msa_logout, msa_signed_in, msa_gamertag
from .config import (
    LOGS, PRETTY, VERSION, get_install_location, clear_install_location,
    default_install_location, is_relocation_allowed,
)
from .relocation import migrate_data, paths_overlap, DIRS_TO_MOVE, FILES_TO_MOVE
from .content import game_content_dir, import_content
from .doctor import acknowledge_gpu_crash, gpu_crash_acknowledgement_status
from .games import list_editions, list_versions
from .gamesetup import do_setup
from .inject import run_injector
from .launch import direct_launch_readiness, launch, single_window_session
from . import log
from .log import BolError, _LEVELS, desktop_notify, warn
from .prefix import _mc_running, kill_wine, prefix_operation_lock, reset_prefix
from .profiles import (
    create_profile, current_profile_info, current_profile_name, delete_profile,
    list_profiles, play_launch_command, profile_launch_command,
    profile_shortcuts_supported, relaunch_with_profile, open_profile_window,
    rename_profile, require_profile_shortcuts_supported,
    require_shortcuts_supported, write_play_shortcut, write_profile_shortcut,
)
from .update import check_for_update, self_update
from .util import load_settings, save_settings, format_display_version, mc_releases, gh_releases

RE_MD_TOKENS = re.compile(r"(\*\*|`|__|\[[^\]]+\]\([^)]+\))")
RE_MD_LINK = re.compile(r"^\[([^\]]+)\]\(([^)]+)\)$")

try:
    import shiboken6
except ImportError:  # pragma: no cover - shiboken6 ships alongside PySide6
    shiboken6 = None


def _alive(widget) -> bool:
    """True if `widget`'s underlying C++ object hasn't been destroyed.

    Worker threads and QTimer.singleShot callbacks are asynchronous: they
    can still be pending when the window that owns them goes away (the
    window closed, or torn down between tests, while a background job is
    in flight). Without this guard the callback runs anyway and touches a
    widget whose C++ side is already gone, raising "libshiboken: Internal
    C++ object already deleted" from inside the Qt event loop.
    """
    if widget is None:
        return False
    if shiboken6 is None:
        return True
    return shiboken6.isValid(widget)


def _desktop_error(message: str) -> None:
    warn(message)
    desktop_notify(message)


# ======================================================================
# Stale-display (XWayland) recovery
#
# Under Wayland, $DISPLAY commonly points at an XWayland X11 socket that can
# go stale (e.g. XWayland restarts between login and launch). The old Tk GUI
# recovered by constructing CTk() and catching the resulting TclError, then
# retrying against another of the user's own X11 sockets.
#
# Qt's xcb platform plugin cannot be recovered the same way: on a failed
# server connection it logs "could not connect to display" and aborts the
# process natively, before control ever returns to Python -- there is no
# catchable exception to retry on. So this probes and repoints $DISPLAY
# *before* QApplication is ever constructed, by connecting directly to the
# candidate X11 sockets, rather than construct-and-catch.
# ======================================================================


def _owned_x11_socket_displays(socket_dir=None, uid=None):
    """Numeric-sorted (":N", ...) tuple of X11 sockets under socket_dir that
    are actually AF_UNIX sockets owned by uid (defaults to the current user
    and /tmp/.X11-unix)."""
    if socket_dir is None:
        socket_dir = Path("/tmp/.X11-unix")
    else:
        socket_dir = Path(socket_dir)
    if uid is None:
        uid = os.getuid()
    try:
        entries = list(socket_dir.iterdir())
    except OSError:
        return ()
    displays = []
    for entry in entries:
        name = entry.name
        if not name.startswith("X") or not name[1:].isdigit():
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        if not stat.S_ISSOCK(st.st_mode) or st.st_uid != uid:
            continue
        displays.append(int(name[1:]))
    return tuple(f":{n}" for n in sorted(displays))


def _x11_socket_is_live(socket_dir, display, timeout=0.5):
    """True if `display` (e.g. ':2') has a socket under socket_dir that
    actually accepts a connection -- a bound-but-unlistened or orphaned
    socket file is not enough."""
    if not display or not display.startswith(":"):
        return False
    num = display[1:].split(".", 1)[0]
    if not num.isdigit():
        return False
    path = Path(socket_dir) / f"X{num}"
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(timeout)
        probe.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def _resolve_gui_display(environ=None, socket_dir=None, uid=None,
                          attempted=None):
    """Repoint environ['DISPLAY'] at a live, user-owned X11 socket before Qt
    ever tries to connect. Only probes when WAYLAND_DISPLAY is set (a pure
    X11 session has nothing more useful to recover to), and only moves off
    the current DISPLAY if it is not actually live. Returns the display
    string that should be used (also written back into environ when it
    changes); never raises."""
    if environ is None:
        environ = os.environ
    if socket_dir is None:
        socket_dir = Path("/tmp/.X11-unix")
    if uid is None:
        uid = os.getuid()
    current = environ.get("DISPLAY")
    if not environ.get("WAYLAND_DISPLAY"):
        return current
    if attempted is not None:
        attempted.append(current or "<unset>")
    if current and _x11_socket_is_live(socket_dir, current):
        return current
    for candidate in _owned_x11_socket_displays(socket_dir, uid=uid):
        if candidate == current:
            continue
        if attempted is not None:
            attempted.append(candidate)
        if _x11_socket_is_live(socket_dir, candidate):
            environ["DISPLAY"] = candidate
            return candidate
    return current


def icon_candidates(module_file=None):
    """Where data/icon.png can be, in the order to try.

    Every packaging layout puts it somewhere different, and one of them has
    no copy next to bol/ at all: the Flatpak installs only the themed icon
    under /app, so dropping that path leaves the window, the title bar and
    the hero screen with no icon at all on the Flathub build.
    """
    here = Path(module_file or __file__).resolve().parent
    return (
        # source checkout, AppImage (usr/bin/data), .deb and .rpm
        # (/usr/lib/bedrock-on-linux/data)
        here.parent / "data/icon.png",
        here / "data/icon.png",
        # Flatpak: the manifest installs the icon under the app-id name only
        Path("/app/share/icons/hicolor/256x256/apps/"
             "io.github.wyze3306.BedrockOnLinux.png"),
        # system icon theme, for a distribution package that ships only that
        Path("/usr/share/icons/hicolor/256x256/apps/bedrock-on-linux.png"),
    )


# ======================================================================
# Theme
# ======================================================================

@dataclass
class Theme:
    """Palette used to generate the app's QSS."""
    dark: bool = True
    beta: bool = False

    def _pick(self, light, dark):
        return dark if self.dark else light

    @property
    def bg(self):        return self._pick("#eef1f6", "#0d0f14")
    @property
    def fg(self):         return self._pick("#12141a", "#eef1f6")
    @property
    def sub(self):        return self._pick("#5a6273", "#9198ab")
    @property
    def muted(self):      return self._pick("#8890a1", "#5a6273")
    @property
    def card(self):       return self._pick("#ffffff", "#161922")
    @property
    def card2(self):      return self._pick("#e6e9f0", "#1d212c")
    @property
    def card3(self):      return self._pick("#d6dae4", "#272c39")
    @property
    def border(self):     return self._pick("#cdd2de", "#2a2f3d")
    @property
    def red(self):        return "#e0574a"
    @property
    def red_hov(self):    return "#c94b3f"
    @property
    def green(self):      return self._pick("#43a047", "#43a047")
    @property
    def green_hov(self):  return self._pick("#3b8e3f", "#4fc153")
    @property
    def green_dim(self):  return self._pick("#e6f4e6", "#1c2c1c")
    @property
    def gold(self):       return self._pick("#d8a230", "#e3b34a")
    @property
    def gold_hov(self):   return self._pick("#c2912a", "#f3c35a")
    @property
    def gold_dim(self):   return self._pick("#fcf3e1", "#33291a")
    @property
    def blue(self):       return "#4a90d9"
    @property
    def blue_dim(self):   return self._pick("#e7f1fb", "#132433")
    @property
    def accent(self):     return self.gold if self.beta else self.green
    @property
    def accent_hov(self): return self.gold_hov if self.beta else self.green_hov
    @property
    def accent_dim(self): return self.gold_dim if self.beta else self.green_dim
    @property
    def console_bg(self): return self._pick("#f7f9fb", "#0a0c10")
    @property
    def console_fg(self): return self._pick("#2f9a5c", "#7fe0a0")

    def qss(self) -> str:
        return f"""
        QWidget {{
            background: transparent;
            color: {self.fg};
            font-family: -apple-system, "Segoe UI", "Inter", sans-serif;
            font-size: 13px;
        }}
        QMainWindow, #Root {{ background: {self.bg}; }}
        QFrame#Card {{
            background: {self.card};
            border: 1px solid {self.border};
            border-radius: 18px;
        }}
        QFrame#CardFlat {{
            background: {self.card};
            border: 1px solid {self.border};
            border-radius: 14px;
        }}
        QFrame#Pill {{
            background: {self.card2};
            border-radius: 14px;
        }}
        QFrame#PillOnCard {{
            background: {self.card2};
            border-radius: 12px;
        }}
        QLabel#Title {{ font-size: 16px; font-weight: 700; }}
        QLabel#Sub {{ color: {self.sub}; }}
        QLabel#Muted {{ color: {self.muted}; font-size: 11px; }}
        QLabel#Hero {{ font-size: 26px; font-weight: 700; }}
        QLabel#Chip {{
            color: {self.accent};
            background: {self.accent_dim};
            border-radius: 9px;
            padding: 4px 12px;
            font-weight: 700;
        }}
        QPushButton {{
            border: none;
            border-radius: 10px;
            padding: 6px 14px;
            background: {self.card2};
            color: {self.fg};
        }}
        QPushButton:hover {{ background: {self.card3}; }}
        QPushButton#Play {{
            background: {self.accent};
            color: white;
            font-weight: 700;
            font-size: 15px;
            border-radius: 12px;
        }}
        QPushButton#Play:hover {{ background: {self.accent_hov}; }}
        QPushButton#Kill {{
            background: {self.red};
            color: white;
            font-weight: 700;
            font-size: 15px;
            border-radius: 12px;
        }}
        QPushButton#Kill:hover {{ background: {self.red_hov}; }}
        QPushButton#Primary {{
            background: {self.accent};
            color: white;
            font-weight: 700;
        }}
        QPushButton#Primary:hover {{ background: {self.accent_hov}; }}
        QPushButton#Danger {{ background: {self.red}; color: white; }}
        QPushButton#Danger:hover {{ background: {self.red_hov}; }}
        QPushButton#Ghost {{ background: transparent; color: {self.sub}; }}
        QPushButton#Ghost:hover {{ background: {self.card2}; color: {self.fg}; }}
        QPushButton#IconBtn {{
            background: {self.card2};
            border-radius: 8px;
        }}
        QPushButton#IconBtn:hover {{ background: {self.card3}; }}
        QPushButton#ToolRow {{
            background: {self.card2};
            border-radius: 10px;
            padding: 10px 14px;
            text-align: left;
            font-size: 13px;
            font-weight: 600;
        }}
        QPushButton#ToolRow:hover {{ background: {self.card3}; }}
        QPushButton#ToolRowDanger {{
            background: {self.card2};
            color: {self.red};
            border-radius: 10px;
            padding: 10px 14px;
            text-align: left;
            font-size: 13px;
            font-weight: 600;
        }}
        QPushButton#ToolRowDanger:hover {{ background: {self.red}; color: white; }}
        QPushButton#Toggle {{
            background: transparent;
            color: {self.sub};
            border-radius: 8px;
            font-weight: 700;
        }}
        QPushButton#Toggle:checked {{
            background: {self.accent_dim};
            color: {self.accent};
        }}
        QLineEdit, QPlainTextEdit {{
            background: {self.card2};
            border: 1px solid transparent;
            border-radius: 10px;
            padding: 6px 10px;
            selection-background-color: {self.accent};
        }}
        QLineEdit:focus {{ border: 1px solid {self.accent}; }}
        QListWidget {{
            background: {self.card2};
            border: none;
            border-radius: 8px;
            outline: none;
        }}
        QListWidget::item {{
            padding: 6px 10px;
            border-radius: 6px;
        }}
        QListWidget::item:selected {{
            background: {self.accent_dim};
            color: {self.accent};
        }}
        QListWidget::item:hover {{ background: {self.card3}; }}
        QProgressBar {{
            background: {self.card2};
            border-radius: 4px;
            height: 8px;
            text-align: center;
            color: transparent;
        }}
        QProgressBar::chunk {{ background: {self.accent}; border-radius: 4px; }}
        QTabWidget::pane {{
            border: none;
            background: {self.bg};
            border-radius: 12px;
            top: 4px;
        }}
        QTabBar {{ background: transparent; }}
        QTabBar::tab {{
            background: {self.card2};
            color: {self.sub};
            padding: 8px 18px;
            border-radius: 8px;
            margin: 4px 3px 4px 0px;
            font-weight: 600;
        }}
        QTabBar::tab:hover {{ background: {self.card3}; color: {self.fg}; }}
        QTabBar::tab:selected {{ background: {self.accent}; color: white; }}
        QScrollArea, QScrollArea > QWidget > QWidget {{ border: none; background: transparent; }}
        QScrollBar:vertical {{ width: 10px; background: transparent; }}
        QScrollBar::handle:vertical {{
            background: {self.card3}; border-radius: 5px; min-height: 24px;
        }}
        QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
        QTextBrowser {{
            background: {self.card2};
            border-radius: 12px;
            border: none;
        }}
        QMessageBox {{ background: {self.card}; }}
        #ActivityLog QTextEdit {{
            background: {self.console_bg};
            color: {self.console_fg};
            font-family: monospace;
            border-radius: 12px;
        }}
        #Popup {{
            background: {self.card2};
            border: 1px solid {self.border};
            border-radius: 12px;
        }}
        """


# ======================================================================
# Background workers
# ======================================================================

class Worker(QThread):
    """Run an arbitrary callable off the UI thread."""
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int)

    def __init__(self, fn: Callable, *args, **kwargs):
        super().__init__()
        self._fn = fn
        self._args = args
        self._kwargs = kwargs

    def _takes_progress(self):
        """Whether the callable accepts a `progress` keyword.

        Read from the signature, not from __code__.co_varnames: co_varnames
        lists local variables as well as parameters, so a callee that merely
        assigns to a name called `progress` would be handed a keyword it
        cannot take -- and the resulting TypeError arrives through failed(),
        where it reads as a real failure of the work itself. Builtins and
        functools.partial have no __code__ at all.
        """
        try:
            parameters = inspect.signature(self._fn).parameters
        except (TypeError, ValueError):
            return False
        if "progress" in parameters:
            return True
        return any(p.kind is inspect.Parameter.VAR_KEYWORD
                   for p in parameters.values())

    def run(self):
        try:
            kwargs = dict(self._kwargs)
            if self._takes_progress():
                kwargs["progress"] = self._emit_progress
            self.done.emit(self._fn(*self._args, **kwargs))
        except Exception as exc:  # noqa: BLE001 - surfaced to the UI
            self.failed.emit(str(exc) or type(exc).__name__)

    def _emit_progress(self, got, total):
        self.progress.emit(got, total)


class LogBridge(QObject):
    """Marshals ``log._LOG_SINK`` calls (any thread) onto the UI thread."""
    line = Signal(str)


# ======================================================================
# Small reusable widgets
# ======================================================================

class Tooltip:
    """Thin wrapper so call sites read the same as the old ``explain()``."""
    def __init__(self, widget: QWidget, text: str):
        widget.setToolTip(text)
        self.widget = widget

    @property
    def text(self):
        return self.widget.toolTip()

    @text.setter
    def text(self, value):
        self.widget.setToolTip(value)


def btn(text, cmd=None, kind="ghost", w=None, h=32, tip=None, parent=None) -> QPushButton:
    b = QPushButton(text, parent)
    b.setObjectName({
        "play": "Play", "primary": "Primary", "danger": "Danger",
        "ghost": "Ghost", "flat": "Ghost", "icon": "IconBtn",
        "toolrow": "ToolRow", "toolrow-danger": "ToolRowDanger",
    }.get(kind, "Ghost"))
    if cmd:
        b.clicked.connect(cmd)
    if w:
        b.setFixedWidth(w)
    b.setFixedHeight(h)
    b.setCursor(Qt.PointingHandCursor)
    if tip:
        b.setToolTip(tip)
    return b


def tool_row(text, cmd, tip=None, danger=False) -> QPushButton:
    """A full-width, left-aligned action row for Settings ▸ Tools."""
    return btn(text, cmd, kind="toolrow-danger" if danger else "toolrow", h=44, tip=tip)


def card_section(parent_layout, title, desc=None) -> QVBoxLayout:
    """A titled settings card, mirroring ``_settings_card`` from the Tk GUI."""
    card = QFrame()
    card.setObjectName("CardFlat")
    v = QVBoxLayout(card)
    v.setContentsMargins(16, 14, 16, 14)
    v.setSpacing(6)
    head = QLabel(title)
    head.setObjectName("Title")
    head.setStyleSheet("font-size:13px;")
    v.addWidget(head)
    if desc:
        d = QLabel(desc)
        d.setObjectName("Sub")
        d.setWordWrap(True)
        d.setStyleSheet("font-size:11px;")
        v.addWidget(d)
    body = QVBoxLayout()
    body.setSpacing(8)
    v.addLayout(body)
    parent_layout.addWidget(card)
    return body


class ToggleSwitch(QAbstractButton):
    """A painted pill-and-knob switch."""

    def __init__(self, theme: "Theme", parent=None):
        super().__init__(parent)
        self._theme = theme
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(42, 24)

    def set_theme(self, theme: "Theme"):
        self._theme = theme
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(1, 1, -1, -1)
        on = self.isChecked()
        track = QColor(self._theme.accent if on else self._theme.card3)
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
        knob_d = rect.height() - 4
        x = rect.right() - knob_d - 2 if on else rect.left() + 2
        p.setBrush(QColor("#ffffff"))
        p.drawEllipse(x, rect.top() + 2, knob_d, knob_d)


class SwitchRow(QWidget):
    """A labelled toggle row used everywhere in Settings."""
    toggled = Signal(bool)

    def __init__(self, text, checked=False, tip=None, theme: Optional["Theme"] = None):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        label = QLabel(text)
        lay.addWidget(label)
        lay.addStretch(1)
        self.switch = ToggleSwitch(theme or Theme())
        self.switch.setChecked(checked)
        self.switch.toggled.connect(self.toggled)
        lay.addWidget(self.switch)
        if tip:
            self.setToolTip(tip)
            label.setToolTip(tip)
            self.switch.setToolTip(tip)

    def isChecked(self):
        return self.switch.isChecked()


class Popup(QFrame):
    """A borderless floating panel positioned relative to an anchor widget."""

    def __init__(self, parent, width=260, height=300):
        super().__init__(parent, Qt.Popup)
        self.setObjectName("Popup")
        self.setFixedSize(width, height)
        self.setAttribute(Qt.WA_TranslucentBackground, False)

    def show_below(self, anchor: QWidget, gap=4):
        pos = anchor.mapToGlobal(QPoint(0, anchor.height() + gap))
        self.move(pos)
        self.show()

    def show_above(self, anchor: QWidget, gap=4):
        pos = anchor.mapToGlobal(QPoint(0, -self.height() - gap))
        self.move(pos)
        self.show()


# ======================================================================
# Version picker
# ======================================================================

class VersionPicker(Popup):
    picked = Signal(str)

    def __init__(self, parent):
        super().__init__(parent, width=260, height=320)
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter versions…")
        v.addWidget(self.search)
        self.list = QListWidget()
        v.addWidget(self.list)
        self.search.textChanged.connect(self._filter)
        self.list.itemClicked.connect(lambda it: self.picked.emit(it.text()))
        self._labels = []

    def set_labels(self, labels, current):
        self._labels = labels
        self.list.clear()
        for lab in labels:
            it = QListWidgetItem(lab)
            self.list.addItem(it)
            if lab == current:
                self.list.setCurrentItem(it)
        self.search.clear()

    def showEvent(self, e):
        super().showEvent(e)
        # A Qt::Popup takes focus itself, so without this the filter field
        # ignores everything typed until it is clicked.
        self.search.setFocus(Qt.PopupFocusReason)

    def _filter(self, text):
        text = text.strip().lower()
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setHidden(bool(text) and text not in it.text().lower())

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter):
            for i in range(self.list.count()):
                it = self.list.item(i)
                if not it.isHidden():
                    self.picked.emit(it.text())
                    return
        elif e.key() == Qt.Key_Escape:
            self.close()
        else:
            super().keyPressEvent(e)


class ProfileMenu(Popup):
    switch = Signal(object)   # profile path or None for default
    new_window = Signal(object)
    create_profile = Signal()
    manage = Signal()

    def __init__(self, parent):
        super().__init__(parent, width=260, height=260)
        self._v = QVBoxLayout(self)
        self._v.setContentsMargins(6, 6, 6, 6)
        self._v.setSpacing(2)

    def rebuild(self, profiles, active_path):
        # setParent(None) as well as deleteLater(): deleteLater only schedules
        # the destruction, so until the event loop next turns, the old rows are
        # still children of this popup -- connected to the same signals, and
        # findable. rebuild() runs immediately before show(), so detaching now
        # is what makes the menu on screen the menu that was just built.
        while self._v.count():
            item = self._v.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        def add_row(name, path, active):
            row = QWidget()
            h = QHBoxLayout(row)
            h.setContentsMargins(0, 0, 0, 0)
            b = btn(name, lambda: self.switch.emit(path), kind="ghost", h=30)
            b.setStyleSheet("text-align:left;" + ("font-weight:700;color:%s;" % "#37b06b" if active else ""))
            h.addWidget(b, 1)
            wb = btn("New Win", lambda: self.new_window.emit(path), kind="ghost", w=64, h=30,
                     tip=f"Open {name} in a new window")
            h.addWidget(wb)
            self._v.addWidget(row)

        add_row("Default", None, active_path is None)
        for p in profiles:
            path = p.get("path")
            is_active = active_path is not None and str(Path(active_path).resolve()) == str(Path(path).resolve())
            add_row(p.get("name", ""), path, is_active)

        div = QFrame()
        div.setFixedHeight(1)
        div.setStyleSheet("background:#5a6273;")
        self._v.addWidget(div)
        self._v.addWidget(btn("+ New Profile…", lambda: self.create_profile.emit(), kind="ghost", h=30))
        self._v.addWidget(btn("Manage Profiles…", lambda: self.manage.emit(), kind="ghost", h=30))
        self._v.addStretch(1)


class GearButton(QAbstractButton):
    """A drawn settings-gear icon (painted, not a font glyph/emoji)."""

    def __init__(self, theme: "Theme", parent=None):
        super().__init__(parent)
        self._theme = theme
        self.setFixedSize(52, 52)
        self.setCursor(Qt.PointingHandCursor)
        self.setAttribute(Qt.WA_Hover, True)
        self.setToolTip("Settings")

    def set_theme(self, theme: "Theme"):
        self._theme = theme
        self.update()

    def paintEvent(self, _event):
        import math
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rect = self.rect()
        bg = QColor(self._theme.card3 if self.underMouse() else self._theme.card2)
        p.setPen(Qt.NoPen)
        p.setBrush(bg)
        p.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 12, 12)

        cx, cy = rect.center().x(), rect.center().y()
        outer_r, inner_r, hole_r, teeth = 11.5, 8.0, 4.2, 8
        p.setBrush(QColor(self._theme.fg))
        points = [
            QPointF(cx + (outer_r if i % 2 == 0 else inner_r) * math.cos(math.pi * i / teeth),
                    cy + (outer_r if i % 2 == 0 else inner_r) * math.sin(math.pi * i / teeth))
            for i in range(teeth * 2)
        ]
        p.drawPolygon(points)
        p.setBrush(bg)
        p.drawEllipse(QPointF(cx, cy), hole_r, hole_r)


# ======================================================================
# Main window
# ======================================================================

def window_action_for_launch(settings, single_window):
    if (settings or {}).get("close_on_launch", False):
        return "close"
    return "step-aside" if single_window else "stay"


class LaunchWorker(QThread):
    """Runs setup + launch, and tells the UI what to do to its own window
    (close it, step aside for a single-window session, come back) once the
    game process actually exists."""
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(int, int)
    close_window = Signal()
    step_aside = Signal()
    come_back = Signal()

    def __init__(self, ver):
        super().__init__()
        self._ver = ver

    def run(self):
        try:
            do_setup(mc_edition=self._ver["edition"], mc_version=self._ver["tag"],
                      progress=lambda g, t: self.progress.emit(g, t))
            action = window_action_for_launch(load_settings(), single_window_session())

            def on_started():
                if action == "close":
                    self.close_window.emit()
                elif action == "step-aside":
                    self.step_aside.emit()

            try:
                launch(on_started=on_started)
            finally:
                self.come_back.emit()
            self.done.emit("closed")
        except Exception as exc:
            self.come_back.emit()
            self.failed.emit(str(exc) or type(exc).__name__)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = load_settings()
        self.theme = Theme(dark=not self.settings.get("light_theme", False),
                            beta=self.settings.get("ui_is_beta", False))

        self.ui_state = {
            "versions": [], "labels": [], "busy": False, "details": False,
            "launch_active": False, "window_gone": False, "stepped_aside": False,
        }
        self._force_close = False
        # slot name -> the QThread currently held for it; see _start_worker.
        self._workers: dict[str, QThread] = {}
        self.na = NativeAuth()
        self._switches: list[SwitchRow] = []
        self._log_bridge = LogBridge()
        self._log_bridge.line.connect(self._on_log_line)
        log._LOG_SINK = lambda m: self._log_bridge.line.emit(m)

        self.setWindowTitle(PRETTY)
        self.resize(1000, 660)
        self.setMinimumSize(880, 640)

        self._load_icon()

        root = QWidget()
        root.setObjectName("Root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(22, 18, 22, 16)
        outer.setSpacing(8)

        outer.addLayout(self._build_topbar())

        self.stack = QStackedWidget()
        outer.addWidget(self.stack, 1)
        self.hero_page = self._build_hero()
        self.settings_page = self._build_settings()
        self.changelog_page = self._build_changelog()
        self.stack.addWidget(self.hero_page)
        self.stack.addWidget(self.settings_page)
        self.stack.addWidget(self.changelog_page)
        self.stack.setCurrentWidget(self.hero_page)

        self.status_row = self._build_status_row()
        outer.addLayout(self.status_row)

        outer.addWidget(self._build_dock())

        self.log_drawer = self._build_log_drawer()
        outer.addWidget(self.log_drawer)
        self.log_drawer.hide()

        self.apply_theme()
        self._wire_version_picker()
        self._wire_profile_menu()
        self._refresh_account_row("in" if msa_signed_in() else "out")

        self._changelog_loaded = False

        QTimer.singleShot(50, self.refresh_versions)
        QTimer.singleShot(200, self.check_for_update_async)

        if self.settings.get("show_changelog_on_startup", False):
            QTimer.singleShot(0, self.toggle_changelog)

    def _start_worker(self, slot, worker) -> bool:
        """Start `worker`, holding the only reference the GUI thread keeps.

        A QThread whose last Python reference goes away is destroyed by the
        C++ side while it is still running, which Qt reports as "QThread:
        Destroyed while thread is still running" and then aborts on. Every
        background job here was stored in a plain attribute, so triggering
        the same action twice -- two clicks on Import, a quick Stable/Preview
        toggle -- overwrote a running thread with its successor.

        Slots are never emptied, only replaced once idle: dropping the
        reference from finished() would put it back in the same race it
        exists to prevent. Returns False when the slot is still busy, which
        is also how repeat clicks are refused.
        """
        previous = self._workers.get(slot)
        if previous is not None and previous.isRunning():
            return False
        self._workers[slot] = worker
        worker.start()
        return True

    # ------------------------------------------------------------ icon
    def _load_icon(self):
        for candidate in icon_candidates():
            if candidate.exists():
                self.icon_pixmap = QPixmap(str(candidate))
                self.setWindowIcon(QIcon(self.icon_pixmap))
                return
        self.icon_pixmap = None

    # ------------------------------------------------------------ theme
    def _switch(self, text, checked=False, tip=None) -> SwitchRow:
        """Themed toggle row; tracked so a later theme change repaints it."""
        row = SwitchRow(text, checked, tip, theme=self.theme)
        self._switches.append(row)
        return row

    def apply_theme(self):
        self.theme.beta = self.settings.get("ui_is_beta", False)
        self.theme.dark = not self.settings.get("light_theme", False)
        QApplication.instance().setStyleSheet(self.theme.qss())
        self._paint_edition_toggle()
        for row in getattr(self, "_switches", ()):
            row.switch.set_theme(self.theme)
        if getattr(self, "settings_btn", None):
            self.settings_btn.set_theme(self.theme)

    # ------------------------------------------------------------ top bar
    def _build_topbar(self) -> QHBoxLayout:
        row = QHBoxLayout()

        brand = QFrame(); brand.setObjectName("Pill")
        bl = QHBoxLayout(brand); bl.setContentsMargins(10, 6, 10, 6)
        icon_lbl = QLabel()
        if self.icon_pixmap:
            icon_lbl.setPixmap(self.icon_pixmap.scaled(24, 24, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        bl.addWidget(icon_lbl)
        whats_new = QToolButton(); whats_new.setText("What's New")
        whats_new.setCursor(Qt.PointingHandCursor)
        whats_new.setStyleSheet("font-weight:700; border:none; background:transparent;")
        whats_new.clicked.connect(self.toggle_changelog)
        bl.addWidget(whats_new)
        ver_lbl = QLabel(f"v{VERSION}"); ver_lbl.setObjectName("Sub")
        bl.addWidget(ver_lbl)
        gh_btn = btn("GitHub", self._open_github, kind="ghost", h=26)
        bl.addWidget(gh_btn)
        row.addWidget(brand)
        row.addStretch(1)

        # Profile switcher pill
        self.prof_card = QFrame(); self.prof_card.setObjectName("Pill")
        self.prof_card.setCursor(Qt.PointingHandCursor)
        pl = QHBoxLayout(self.prof_card); pl.setContentsMargins(14, 6, 10, 6)
        self.prof_label = QLabel(f"Profile: {current_profile_name()}")
        pl.addWidget(self.prof_label)
        pl.addWidget(QLabel("▾"))
        self.prof_card.mousePressEvent = lambda e: self.open_profile_menu()
        row.addWidget(self.prof_card)

        # Account pill
        acct = QFrame(); acct.setObjectName("Pill")
        al = QHBoxLayout(acct); al.setContentsMargins(14, 6, 8, 6)
        self.acct_dot = QLabel("●")
        al.addWidget(self.acct_dot)
        self.acct_text = QLabel("Not signed in")
        al.addWidget(self.acct_text)
        self.acct_btn = btn("Sign in", self.acct_click, kind="ghost", w=88, h=30)
        al.addWidget(self.acct_btn)
        row.addWidget(acct)

        self.update_banner_slot = row
        return row

    def _open_github(self):
        subprocess.Popen(["xdg-open", "https://github.com/Wyze3306/BedrockOnLinux"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # ------------------------------------------------------------ hero
    def _build_hero(self) -> QWidget:
        card = QFrame(); card.setObjectName("Card")
        v = QVBoxLayout(card)
        v.setAlignment(Qt.AlignCenter)
        if self.icon_pixmap:
            lbl = QLabel()
            lbl.setPixmap(self.icon_pixmap.scaled(118, 118, Qt.KeepAspectRatio, Qt.SmoothTransformation))
            lbl.setAlignment(Qt.AlignCenter)
            v.addWidget(lbl)
        title = QLabel("Minecraft Bedrock"); title.setObjectName("Hero")
        title.setAlignment(Qt.AlignCenter)
        v.addWidget(title)
        sub = QLabel("Bedrock Edition for Linux"); sub.setObjectName("Sub")
        sub.setAlignment(Qt.AlignCenter)
        v.addWidget(sub)
        self.selected_chip = QLabel(""); self.selected_chip.setObjectName("Chip")
        self.selected_chip.setAlignment(Qt.AlignCenter)
        v.addWidget(self.selected_chip, 0, Qt.AlignCenter)
        return card

    # ------------------------------------------------------------ status row
    def _build_status_row(self) -> QVBoxLayout:
        col = QVBoxLayout()
        self.status_label = QLabel("Ready to play.")
        self.status_label.setObjectName("Sub")
        col.addWidget(self.status_label)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.hide()
        col.addWidget(self.progress)
        return col

    # ------------------------------------------------------------ dock
    def _build_dock(self) -> QFrame:
        dock = QFrame(); dock.setObjectName("Card")
        h = QHBoxLayout(dock)
        h.setContentsMargins(16, 14, 16, 14)

        # Version field
        self.ver_field = QFrame(); self.ver_field.setObjectName("PillOnCard")
        self.ver_field.setFixedSize(220, 52)
        self.ver_field.setCursor(Qt.PointingHandCursor)
        vfl = QHBoxLayout(self.ver_field); vfl.setContentsMargins(14, 0, 12, 0)
        self.ver_label = QLabel("Loading…")
        vfl.addWidget(self.ver_label, 1)
        vfl.addWidget(QLabel("▾"))
        self.ver_field.mousePressEvent = lambda e: self.open_picker()
        h.addWidget(self.ver_field)

        # Edition toggle
        ed_field = QFrame(); ed_field.setObjectName("PillOnCard")
        ed_field.setFixedHeight(52)
        efl = QHBoxLayout(ed_field); efl.setContentsMargins(6, 8, 6, 8)
        self.edition_group = QButtonGroup(self)
        self.edition_group.setExclusive(True)
        self.stable_btn = btn("Stable", None, kind="ghost", w=78, h=32)
        self.preview_btn = btn("Preview", None, kind="ghost", w=78, h=32)
        for b in (self.stable_btn, self.preview_btn):
            b.setObjectName("Toggle")
            b.setCheckable(True)
        self.edition_group.addButton(self.stable_btn)
        self.edition_group.addButton(self.preview_btn)
        efl.addWidget(self.stable_btn)
        efl.addWidget(self.preview_btn)
        self.stable_btn.clicked.connect(lambda: self.select_edition("release"))
        self.preview_btn.clicked.connect(lambda: self.select_edition("preview"))
        h.addWidget(ed_field)

        h.addStretch(1)

        self.details_btn = btn("Details", self.toggle_details, kind="ghost", w=76, h=52,
                                tip="Show Activity Logs")
        h.addWidget(self.details_btn)
        self.settings_btn = GearButton(self.theme)
        self.settings_btn.clicked.connect(self.toggle_settings)
        h.addWidget(self.settings_btn)
        self.play_btn = btn("▶  PLAY", self.do_play, kind="play", w=120, h=52, tip="Play Game")
        h.addWidget(self.play_btn)

        edition_id = "preview" if self.settings.get("show_betas", False) else "release"
        (self.preview_btn if edition_id == "preview" else self.stable_btn).setChecked(True)
        return dock

    def _paint_edition_toggle(self):
        pass  # QSS handles the checked-state colouring via #Toggle:checked

    # ------------------------------------------------------------ log drawer
    def _build_log_drawer(self) -> QFrame:
        wrap = QFrame(); wrap.setObjectName("ActivityLog")
        wrap.setFixedHeight(220)
        v = QVBoxLayout(wrap)
        head = QHBoxLayout()
        lab = QLabel("ACTIVITY LOG"); lab.setObjectName("Muted")
        head.addWidget(lab)
        head.addStretch(1)
        head.addWidget(btn("Clear", lambda: self.log_view.clear(), kind="flat",
                            w=64, h=24, tip="Empty the activity log"))
        # Wide enough for "Copied ✓", which replaces the label on click.
        self.copy_log_btn = btn("Copy", self._copy_log, kind="flat", w=84, h=24,
                                 tip="Copy the whole log, for a bug report")
        head.addWidget(self.copy_log_btn)
        v.addLayout(head)
        self.log_view = QTextEdit(); self.log_view.setReadOnly(True)
        v.addWidget(self.log_view)
        return wrap

    def _copy_log(self):
        QApplication.clipboard().setText(self.log_view.toPlainText())
        self.copy_log_btn.setText("Copied ✓")
        QTimer.singleShot(1200, lambda: _alive(self.copy_log_btn) and self.copy_log_btn.setText("Copy"))

    def toggle_details(self):
        self.ui_state["details"] = not self.ui_state["details"]
        self.log_drawer.setVisible(self.ui_state["details"])

    # ------------------------------------------------------------ logging
    _FRIENDLY = (
        ("downloading minecraft", None),
        ("building winegdk", "Setting up the game engine — first run, this can take a while…"),
        ("cloning winegdk", "Setting up the game engine — first run, this can take a while…"),
        ("updating winegdk", "Setting up the game engine — first run, this can take a while…"),
        ("installing minecraft", "Installing Minecraft…"),
        ("reinstalling minecraft", "Installing Minecraft…"),
        ("preparing gdk-proton", "Preparing the engine…"),
        ("extracting", "Preparing the engine…"),
        ("pre-auth", "Signing in to Xbox Live…"),
        ("signing in", "Signing in to Xbox Live…"),
        ("offline mode", "Starting Minecraft in offline mode…"),
        ("starting minecraft", "Starting Minecraft…"),
        ("launching minecraft", "Starting Minecraft…"),
    )

    def _friendly(self, line: str):
        low = line.lower()
        if "minecraft is running" in low:
            return "Minecraft is running — close the game to come back here.", True
        if "game closed" in low:
            return "Minecraft closed.", True
        for needle, msg in self._FRIENDLY:
            if needle in low:
                return msg, False
        return None

    def set_status(self, text, color=None):
        """The status line, colour included.

        Always writing the stylesheet is the point: a failure paints the line
        red, and nothing that came after it used to paint it back, so one
        failed launch left "Preparing…" and "Downloading…" red for the rest
        of the session.
        """
        self.status_label.setText(text)
        self.status_label.setStyleSheet(
            f"color:{color};" if color else "")

    def _append_log_html(self, markup: str):
        """Append pre-escaped markup and keep the view on the newest line."""
        self.log_view.append(markup)
        bar = self.log_view.verticalScrollBar()
        bar.setValue(bar.maximum())

    @Slot(str)
    def _on_log_line(self, line: str):
        lvl = _LEVELS.get(line[:2])
        if lvl:
            label, _a1, _a2, level_color, msg_color = lvl
            self._append_log_html(
                f'<span style="color:{level_color}; font-weight:700;">{html.escape(label)}</span>'
                f'  <span style="color:{msg_color};">{html.escape(line[2:].strip())}</span>')
        else:
            # The wrapping span is not decoration. QTextEdit.append() decides
            # between rich and plain text with Qt::mightBeRichText(), and an
            # escaped string that contains no tag does not look like markup --
            # so "-> downloading 1.21.130.7" was appended verbatim and rendered
            # as "-&gt; downloading 1.21.130.7". Only `::`, `OK`, `!!` and `xx`
            # carry a level, so `==` and `->` -- the two most common prefixes
            # in a launch -- always took this branch.
            self._append_log_html(f"<span>{html.escape(line)}</span>")
        if not self.ui_state.get("busy"):
            return
        if line.startswith("xx"):
            self.set_status(line[2:].strip(), self.theme.red)
            return
        friendly = self._friendly(line)
        if friendly:
            txt = friendly[0] if isinstance(friendly, tuple) else friendly
            steady = friendly[1] if isinstance(friendly, tuple) else False
            self.set_status(txt, self.theme.green if steady else None)
            if steady:
                self.progress.hide()
            else:
                self._show_bar_busy()

    def _show_bar_busy(self):
        self.progress.show()
        self.progress.setRange(0, 0)  # indeterminate

    def set_progress(self, got, total):
        self.progress.show()
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(got)
        self.set_status(
            f"Downloading Minecraft…  {int(100 * got / max(1, total))}%")

    def end_progress(self):
        self.progress.hide()

    # ------------------------------------------------------------ version picker
    def _wire_version_picker(self):
        self.version_popup = VersionPicker(self)
        self.version_popup.picked.connect(self.set_version)

    def open_picker(self):
        labels = [l for l, v in zip(self.ui_state.get("labels") or [],
                                     self.ui_state.get("versions") or [])
                  if self._edition_matches(v)]
        if not labels:
            return
        self.version_popup.set_labels(labels, self.ver_label.text())
        self.version_popup.setFixedWidth(max(260, self.ver_field.width()))
        self.version_popup.show_above(self.ver_field)

    def set_version(self, label):
        self.version_popup.close()
        self.ver_label.setText(label)
        self._update_selected_chip()

    def _edition_matches(self, v, wanted=None):
        wanted = (wanted or ("preview" if self.preview_btn.isChecked() else "release")) == "preview"
        return bool(v.get("beta", False)) == wanted

    def select_edition(self, edition_id):
        self.settings = load_settings()
        self.settings["show_betas"] = edition_id == "preview"
        save_settings(self.settings)
        matches = [l for l, v in zip(self.ui_state.get("labels") or [],
                                      self.ui_state.get("versions") or [])
                   if self._edition_matches(v, edition_id)]
        if matches:
            self.set_version(matches[0])
        else:
            self.selected_chip.setText("")

    def _update_selected_chip(self):
        lab = self.ver_label.text()
        if not lab or lab == "Loading…":
            self.selected_chip.setText("")
            return
        is_beta = "BETA" in lab
        self.settings = load_settings()
        changed = False
        if self.settings.get("ui_is_beta") != is_beta:
            self.settings["ui_is_beta"] = is_beta
            changed = True
        cur_mc_ver = lab.split("  ")[0]
        if self.settings.get("mc_version") != cur_mc_ver:
            self.settings["mc_version"] = cur_mc_ver
            changed = True
        if changed:
            save_settings(self.settings)
        self.selected_chip.setText(f"  {cur_mc_ver}{'  ·  BETA' if is_beta else ''}  ")
        (self.preview_btn if is_beta else self.stable_btn).setChecked(True)
        self.theme.beta = is_beta
        self.apply_theme()
        cur_kill = self.ui_state.get("busy")
        self.play_btn.setToolTip(f"{'Kill' if cur_kill else 'Play'} {cur_mc_ver}")

    def selected_version(self):
        lab = self.ver_label.text()
        if not self.ui_state["versions"] or not lab:
            return None
        labels = [format_display_version(v["tag"], v["beta"]) + ("  ·  BETA" if v["beta"] else "")
                  for v in self.ui_state["versions"]]
        try:
            idx = labels.index(lab)
            return self.ui_state["versions"][idx]
        except ValueError:
            return None

    def refresh_versions(self):
        def work():
            # Always fetch the full catalogue, stable and preview alike.
            # Which edition is *shown* is a purely client-side filter
            # (_edition_matches / open_picker / select_edition all filter
            # self.ui_state["versions"] after the fact) -- this only runs
            # once per app launch, so gating the fetch itself by the
            # currently-saved show_betas left the other edition's versions
            # never loaded for the rest of the session. That's what made
            # Preview un-selectable after a restart that happened to land
            # on Stable: the version list was fetched with
            # include_beta=False and never refetched, so no amount of
            # clicking "Preview" afterward could produce a match.
            editions = list_editions(include_beta=True)
            versions = []
            for ed in editions:
                # Per edition, so one catalogue being unreachable costs that
                # edition and not the whole picker. Without this a Preview
                # outage left the player with no Stable builds either.
                try:
                    builds = list_versions(ed["id"])
                except Exception as exc:
                    log._LOG_SINK(f"xx versions for {ed['id']}: {exc}")
                    continue
                for b in builds:
                    versions.append({"tag": b["version"], "beta": ed.get("beta", False),
                                      "edition": ed, "installed": b.get("installed", False)})
            return versions

        worker = Worker(work)
        worker.done.connect(self._on_versions_loaded)
        worker.failed.connect(lambda e: log._LOG_SINK(f"xx versions: {e}"))
        self._start_worker("versions", worker)

    def _on_versions_loaded(self, versions):
        if not _alive(self) or not _alive(self.ver_label):
            return
        if not versions:
            log._LOG_SINK("xx no versions loaded")
            return
        self.ui_state["versions"] = versions
        labels = [format_display_version(v["tag"], v["beta"]) + ("  ·  BETA" if v["beta"] else "")
                  for v in versions]
        self.ui_state["labels"] = labels
        cur = load_settings().get("mc_version") or ""
        pick = next((x for x in labels if x.split("  ")[0] == cur
                     or x.split("  ")[0].startswith(cur + ".")), labels[0])
        self.ver_label.setText(pick)
        self._update_selected_chip()

    # ------------------------------------------------------------ profile menu
    def _wire_profile_menu(self):
        self.profile_popup = ProfileMenu(self)
        self.profile_popup.switch.connect(self._switch_profile_target)
        self.profile_popup.new_window.connect(lambda p: open_profile_window(p))
        self.profile_popup.create_profile.connect(self._prompt_create_profile)
        self.profile_popup.manage.connect(self._open_profile_manager)

    def open_profile_menu(self):
        info = current_profile_info()
        self.profile_popup.rebuild(list_profiles(), info.get("path"))
        self.profile_popup.setFixedWidth(max(260, self.prof_card.width()))
        self.profile_popup.show_below(self.prof_card)

    def _profile_switch_blocked(self) -> bool:
        if self.ui_state.get("launch_active"):
            self.warn_box("Minecraft is running",
                "Close Minecraft first and wait for the game to exit before "
                "switching profiles in this window.\n\nThe \"New Win\" button "
                "opens another profile in a second launcher window, but only "
                "one profile can play at a time.")
            return True
        if self.ui_state.get("busy"):
            self.warn_box("Operation in progress",
                "Wait for the current preparation task to finish before "
                "switching profiles.")
            return True
        return False

    def _switch_profile_target(self, profile_path):
        self.profile_popup.close()
        if self._profile_switch_blocked():
            return
        self.close()
        relaunch_with_profile(profile_path)

    def _prompt_create_profile(self):
        self.profile_popup.close()
        if self.ui_state.get("busy") and not self.ui_state.get("launch_active"):
            self.warn_box("Operation in progress",
                "Wait for the current preparation task to finish before "
                "creating a profile.")
            return
        name, ok = QInputDialog.getText(self, "Create account profile",
            "Profile name (each profile has its own Xbox login, prefix and worlds):")
        if not ok or not name.strip():
            return
        name = name.strip()
        try:
            profile_dir = create_profile(name)
            if profile_shortcuts_supported():
                try:
                    write_profile_shortcut(name, profile_dir=profile_dir)
                except Exception:
                    pass
            if self.ui_state.get("launch_active"):
                if self.question_box("Profile Created",
                        f"Profile '{name}' was created successfully.\n\n"
                        "Minecraft is currently running, so this window can't "
                        "switch now. Open the new profile in a new window?"):
                    open_profile_window(profile_dir)
            else:
                self._switch_profile_target(profile_dir)
        except Exception as exc:
            self.error_box("Account profile", str(exc))

    def _open_profile_manager(self):
        self.profile_popup.close()
        dlg = ProfileManagerDialog(self)
        dlg.exec()
        self.prof_label.setText(f"Profile: {current_profile_name()}")

    # ------------------------------------------------------------ account
    def _refresh_account_row(self, phase):
        if not _alive(self) or not _alive(self.acct_dot):
            return
        gt = msa_gamertag() or "Xbox Live"
        if phase == "in":
            self.acct_dot.setStyleSheet(f"color:{self.theme.green};")
            self.acct_text.setText("Signed in")
            self.acct_btn.setText("Sign out")
            self.acct_btn.setToolTip(f"Sign out of {gt}")
            self._acct_mode = "out"
        elif phase == "auth":
            self.acct_dot.setStyleSheet(f"color:{self.theme.gold};")
            self.acct_text.setText("Sign-in pending…")
            self.acct_btn.setText("Cancel")
            self._acct_mode = "cancel"
        else:
            self.acct_dot.setStyleSheet(f"color:{self.theme.sub};")
            self.acct_text.setText("Not signed in")
            self.acct_btn.setText("Sign in")
            self.acct_btn.setToolTip("Sign in to Microsoft")
            self._acct_mode = "in"
        self._acct_confirm = False
        self._watch_for_stray_clicks(False)

    def _watch_for_stray_clicks(self, watching):
        """Install the application-wide mouse filter only while it is needed.

        A question left armed on screen is one the next stray click answers,
        so an armed "Sign out?" has to notice a click that lands anywhere
        else -- which under Qt means an application event filter, the same
        reach the Tk build got from bind_all("<Button-1>").

        It is installed and removed around the armed state rather than for
        the window's lifetime: an application filter is consulted for every
        event delivered anywhere in the process, and that is not a cost worth
        paying for the seconds a confirmation is actually up.
        """
        if watching == getattr(self, "_watching_clicks", False):
            return
        app = QApplication.instance()
        if app is None:
            return
        if watching:
            app.installEventFilter(self)
        else:
            app.removeEventFilter(self)
        self._watching_clicks = watching

    def _arm_account_confirm(self, label):
        """Two-step destructive buttons: the first click asks, the second
        acts."""
        self._acct_confirm = True
        self._watch_for_stray_clicks(True)
        self.acct_btn.setText(label)
        self.acct_btn.setStyleSheet(f"background:{self.theme.red}; color:white;")

    def _disarm_account_confirm(self):
        if not getattr(self, "_acct_confirm", False):
            return
        self.acct_btn.setStyleSheet("")
        self._refresh_account_row(
            {"out": "in", "cancel": "auth"}.get(
                getattr(self, "_acct_mode", "in"), "out"))

    def acct_click(self):
        mode = getattr(self, "_acct_mode", "in")
        if mode == "loading":
            # The device-code request is already in flight. Without this a
            # second click starts a second one, and the player is handed two
            # codes for the same sign-in.
            return
        if mode == "out":
            if getattr(self, "_acct_confirm", False):
                self.na.stop()
                try:
                    msa_logout()
                except BolError as exc:
                    warn(str(exc))
                self._refresh_account_row("in" if msa_signed_in() else "out")
            else:
                self._arm_account_confirm("Sign out?")
        elif mode == "cancel":
            if getattr(self, "_acct_confirm", False):
                self.na.stop()
                self._refresh_account_row("in" if msa_signed_in() else "out")
                if getattr(self, "_auth_dialog", None):
                    self._auth_dialog.close()
            else:
                self._arm_account_confirm("Cancel?")
        else:
            self._acct_mode = "loading"
            self.acct_btn.setText("Loading…")
            threading.Thread(target=lambda: self.na.start(self._on_auth, self._on_online),
                              daemon=True).start()

    def _on_auth(self, url, code):
        QTimer.singleShot(0, lambda: _alive(self) and (
            self._refresh_account_row("auth"), self._code_dialog(url, code)))

    def _on_online(self):
        QTimer.singleShot(0, lambda: _alive(self) and self._refresh_account_row("in"))
        if getattr(self, "_auth_dialog", None) and _alive(self._auth_dialog):
            QTimer.singleShot(0, self._auth_dialog.close)
        self._warm_xbox_preauth()

    def _warm_xbox_preauth(self):
        """Mint the Xbox token chain now rather than at PLAY.

        launch.py runs xbl_preauth again on its own, so nothing breaks
        without this -- it just moves the whole SISU/XSTS round trip off the
        first launch and into the moment the player is already waiting on a
        sign-in. It also settles the account row: the row goes green on the
        MSA token alone, and only this says whether Xbox agreed.
        """
        def work():
            from .auth import (
                msa_load, msa_refresh, xbl_preauth, _account_cache_epoch)
            from .config import DATA
            try:
                token = msa_load()
                if not token:
                    return
                fresh = msa_refresh(token.get("refresh_token"))
                if not (fresh and fresh.get("access_token")):
                    return
                epoch = _account_cache_epoch(DATA / "winegdk-preauth")
                if xbl_preauth(fresh.get("access_token"), epoch):
                    QTimer.singleShot(
                        0, lambda: _alive(self) and self._refresh_account_row("in"))
            except Exception:
                # Best-effort warm-up: PLAY re-runs the whole chain and is
                # where a real failure has to be reported, with its
                # diagnostic attached.
                pass

        threading.Thread(target=work, daemon=True).start()

    def _code_dialog(self, url, code):
        full_url = f"https://login.live.com/oauth20_remoteconnect.srf?otc={code}"
        dlg = QDialog(self)
        dlg.setWindowTitle("Sign in to Microsoft")
        self._auth_dialog = dlg

        def on_close():
            self.na.stop()
            self._refresh_account_row("in" if msa_signed_in() else "out")
            dlg.close()
        dlg.finished.connect(lambda _r: on_close())

        v = QVBoxLayout(dlg)
        v.addWidget(btn("Sign In to your Microsoft account",
                        lambda: subprocess.Popen(["xdg-open", full_url],
                                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
                        kind="primary", h=42))
        v.addWidget(QLabel("Open the link and enter this code:"))
        code_lbl = QLabel(code)
        code_lbl.setAlignment(Qt.AlignCenter)
        code_lbl.setStyleSheet("font-family: monospace; font-size: 28px; font-weight: 700; "
                                f"color: {self.theme.blue};")
        v.addWidget(code_lbl)
        copy_row = QHBoxLayout()
        copy_row.addStretch(1)
        cbtn = btn("Copy code", lambda: QApplication.clipboard().setText(code), kind="ghost")
        copy_row.addWidget(cbtn)
        copy_row.addStretch(1)
        v.addLayout(copy_row)
        dlg.resize(380, 260)
        dlg.show()

    # ------------------------------------------------------------ play / kill
    def do_play(self):
        if self.ui_state["busy"]:
            return
        ver = self.selected_version()
        if ver is None:
            self.warn_box("No version selected", "Pick a Minecraft version first.")
            return
        self._set_busy(True)
        self.set_status("Preparing…")
        self._show_bar_busy()

        w = LaunchWorker(ver)
        w.progress.connect(self.set_progress)
        w.done.connect(self._play_finished)
        w.failed.connect(self._play_failed)
        w.close_window.connect(self._close_for_game)
        w.step_aside.connect(self._step_aside_for_game)
        w.come_back.connect(self._come_back_from_game)
        self.ui_state["launch_active"] = True
        self._start_worker("play", w)

    def _close_for_game(self):
        """The player asked for this in Settings ▸ General: the window goes
        for good the moment the game starts, while this process stays alive
        in the background to see the launch/session out."""
        if self.ui_state.get("window_gone"):
            return
        self.ui_state["window_gone"] = True
        self.na.stop()
        self._watch_for_stray_clicks(False)
        self._force_close = True
        self.close()

    def _step_aside_for_game(self):
        """Single-window sessions (e.g. Steam Game Mode) show one window at
        a time, so hide instead of closing; ``_come_back_from_game`` restores it."""
        if self.ui_state.get("stepped_aside"):
            return
        self.ui_state["stepped_aside"] = True
        self.hide()

    def _come_back_from_game(self):
        if not self.ui_state.get("stepped_aside"):
            return
        self.ui_state["stepped_aside"] = False
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _play_finished(self, _result):
        self.ui_state["launch_active"] = False
        if self.ui_state.get("window_gone"):
            QApplication.instance().quit()
            return
        self.set_status("Minecraft closed.")
        self.end_progress()
        self._set_busy(False)

    def _play_failed(self, message):
        self.ui_state["launch_active"] = False
        log._LOG_SINK(f"xx {message}")
        if self.ui_state.get("window_gone"):
            desktop_notify(message[:400], "Minecraft could not start")
            QApplication.instance().quit()
            return
        self.set_status("Minecraft could not start.", self.theme.red)
        self.end_progress()
        self._set_busy(False)
        try:
            ack = gpu_crash_acknowledgement_status()
        except Exception:
            ack = None
        if ack and ack.can_acknowledge:
            self._offer_gpu_ack(ack, prefix=message[:2000] + "\n\n",
                                 title="Minecraft could not start")
        else:
            self.error_box("Minecraft could not start", message[:2000])

    def _set_busy(self, on):
        self.ui_state["busy"] = on
        if on:
            self.play_btn.setObjectName("Kill")
            self.play_btn.setText("⏹  KILL")
            self.play_btn.clicked.disconnect()
            self.play_btn.clicked.connect(kill_wine)
        else:
            self.play_btn.setObjectName("Play")
            self.play_btn.setText("▶  PLAY")
            self.play_btn.clicked.disconnect()
            self.play_btn.clicked.connect(self.do_play)
        self.play_btn.style().unpolish(self.play_btn)
        self.play_btn.style().polish(self.play_btn)

    def _offer_gpu_ack(self, ack_status, prefix="", title="Acknowledge previous GPU incident"):
        return _offer_gpu_incident_acknowledgement(
            _MainWindowMessageBoxAdapter(self), self, ack_status,
            prefix=prefix, title=title)

    # ------------------------------------------------------------ settings / changelog toggles
    def toggle_settings(self):
        if self.stack.currentWidget() is self.settings_page:
            self.stack.setCurrentWidget(self.hero_page)
        else:
            self.stack.setCurrentWidget(self.settings_page)

    def toggle_changelog(self):
        if self.stack.currentWidget() is self.changelog_page:
            self.stack.setCurrentWidget(self.hero_page)
        else:
            self.stack.setCurrentWidget(self.changelog_page)
            self.load_changelogs()

    # ------------------------------------------------------------ settings page
    def _build_settings(self) -> QWidget:
        page = QFrame(); page.setObjectName("Card")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(20, 18, 20, 18)
        head = QHBoxLayout()
        t = QLabel("Settings"); t.setObjectName("Title")
        head.addWidget(t)
        head.addStretch(1)
        head.addWidget(btn("← Back", self.toggle_settings, kind="flat", w=76, h=28))
        outer.addLayout(head)

        tabs = QTabWidget()
        outer.addWidget(tabs, 1)

        tabs.addTab(self._scrollable(self._build_general_tab()), "General")
        tabs.addTab(self._scrollable(self._build_advanced_tab()), "Advanced")
        tabs.addTab(self._scrollable(self._build_tools_tab()), "Tools")
        return page

    def _scrollable(self, inner: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setWidget(inner)
        return area

    def _build_general_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        appearance = card_section(v, "Appearance")
        theme_row = self._switch("Light theme", self.settings.get("light_theme", False),
                               "Switch the launcher between dark and light appearance.")
        theme_row.toggled.connect(self._save_theme_toggle)
        appearance.addWidget(theme_row)

        startup = card_section(v, "Startup")
        cl_row = self._switch("Show changelog on startup",
                            self.settings.get("show_changelog_on_startup", False),
                            "Open the What's New tab automatically each time the launcher starts.")
        cl_row.toggled.connect(lambda on: self._save_setting("show_changelog_on_startup", on))
        startup.addWidget(cl_row)

        confine_row = self._switch("Keep the mouse inside the window",
                                 self.settings.get("confine_cursor", False),
                                 "Fixes the cursor escaping the game in windowed mode.")
        confine_row.toggled.connect(lambda on: self._save_setting("confine_cursor", on))
        startup.addWidget(confine_row)

        close_row = self._switch("Close the launcher when Minecraft starts",
                               self.settings.get("close_on_launch", False),
                               "The window closes as soon as the game starts, instead of "
                               "waiting for it. Off by default.")
        close_row.toggled.connect(lambda on: self._save_setting("close_on_launch", on))
        startup.addWidget(close_row)

        accounts = card_section(v, "Accounts",
            "Minecraft is downloaded from the Microsoft Store with the account "
            "that owns it — a separate, device-bound session from the "
            "in-game sign-in above.")
        store_row = QHBoxLayout()
        self.store_label = QLabel("Store account: …")
        store_row.addWidget(self.store_label)
        store_row.addStretch(1)
        self.store_btn = btn("Link…", self._toggle_store_account, kind="ghost", w=88, h=28)
        store_row.addWidget(self.store_btn)
        accounts.addLayout(store_row)
        self._refresh_store_row()

        v.addStretch(1)
        return w

    def _save_theme_toggle(self, on):
        self._save_setting("light_theme", on)
        self.apply_theme()

    def _save_setting(self, key, value):
        self.settings = load_settings()
        self.settings[key] = value
        save_settings(self.settings)

    def _refresh_store_row(self):
        if not _alive(self) or not _alive(self.store_label) or not _alive(self.store_btn):
            return
        from . import xodus
        linked = xodus.signed_in()
        self.store_label.setText("Store account: " + ("linked" if linked else "not linked"))
        self.store_btn.setText("Unlink" if linked else "Link…")

    def _toggle_store_account(self):
        from . import xodus

        def work():
            if xodus.signed_in():
                xodus.logout()
            else:
                xodus.login()

        w = Worker(work)
        w.done.connect(lambda _r: self._refresh_store_row())
        w.failed.connect(lambda e: _alive(self) and self.error_box("Microsoft Store account", e))
        self._start_worker("store-account", w)

    def _build_advanced_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        graphics = card_section(v, "Graphics")
        rt = self._switch("Ray tracing", self.settings.get("ray_tracing", True),
                        "Hands DXR to Minecraft for its Ray Traced mode. Needs an "
                        "RTX-class GPU and a ray-tracing-capable world.")
        rt.toggled.connect(lambda on: self._save_setting("ray_tracing", on))
        graphics.addWidget(rt)

        fr = self._switch("Limit frame rate to the display",
                        self.settings.get("limit_frame_rate", True),
                        "Only applies when Minecraft has no limit of its own.")
        fr.toggled.connect(lambda on: self._save_setting("limit_frame_rate", on))
        graphics.addWidget(fr)

        lr = self._switch("Legacy compatibility renderer",
                        self.settings.get("renderer", "auto") == "opengl",
                        "Last resort for GPUs without Vulkan 1.3 — drops DXVK/vkd3d.")
        lr.toggled.connect(lambda on: self._save_setting("renderer", "opengl" if on else "auto"))
        graphics.addWidget(lr)

        env = card_section(v, "Environment")
        env.addWidget(QLabel("Custom environment variables"))
        env_entry = QLineEdit(self.settings.get("custom_env") or "")
        env_entry.setPlaceholderText("e.g., PROTON_USE_WINED3D=1 KEY=VALUE")
        env_entry.textChanged.connect(lambda t: self._save_setting("custom_env", t))
        env.addWidget(env_entry)

        env.addWidget(QLabel("Gamescope arguments"))
        gs_entry = QLineEdit(self.settings.get("gamescope") or "")
        gs_entry.setPlaceholderText("1 for auto, or e.g. -w 1920 -h 1080 -f")
        gs_entry.textChanged.connect(lambda t: self._save_setting("gamescope", t))
        env.addWidget(gs_entry)

        self._build_storage_card(v)

        diagnostics = card_section(v, "Diagnostics")
        diag = self._switch("Advanced diagnostics", self.settings.get("diagnostics", False),
                          "Verbose logs, for attaching to bug reports.")
        diag.toggled.connect(lambda on: self._save_setting("diagnostics", on))
        diagnostics.addWidget(diag)

        v.addStretch(1)
        return w

    def _build_storage_card(self, v):
        storage = card_section(v, "Storage",
            "Where the engine, downloaded Minecraft versions, saves and "
            "settings are stored. Changing this requires a restart.")

        path_row = QHBoxLayout()
        self.loc_label = QLabel(get_install_location())
        self.loc_label.setStyleSheet("font-family: monospace;")
        path_row.addWidget(self.loc_label, 1)
        copy_btn = btn("Copy", self._copy_install_path, kind="ghost", w=54, h=28, tip="Copy path")
        copy_btn.setStyleSheet("font-size:11px;")
        open_btn = btn("Open", self._open_install_folder, kind="ghost", w=54, h=28,
                        tip="Open in file manager")
        open_btn.setStyleSheet("font-size:11px;")
        path_row.addWidget(copy_btn)
        path_row.addWidget(open_btn)
        storage.addLayout(path_row)

        self.free_space_label = QLabel("")
        self.free_space_label.setObjectName("Muted")
        storage.addWidget(self.free_space_label)
        self._refresh_free_space()

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        btn_row.addWidget(btn("Browse…", self._do_browse_location, kind="ghost", w=84, h=32,
                               tip="Choose a new folder and move your existing worlds, "
                                   "settings and login there."))
        btn_row.addWidget(btn("Reset", self._do_reset_location, kind="flat", w=64, h=32,
                               tip="Clear the saved preference and go back to the default location."))
        storage.addLayout(btn_row)

        self.loc_status_label = QLabel("")
        self.loc_status_label.setStyleSheet(f"color:{self.theme.gold};")
        storage.addWidget(self.loc_status_label)

    def _refresh_free_space(self):
        try:
            p = Path(self.loc_label.text())
            check_p = p if p.exists() else p.parent
            free = shutil.disk_usage(check_p).free
            self.free_space_label.setText(f"{self._fmt_size(free)} free on this drive")
        except Exception:
            self.free_space_label.setText("")

    @staticmethod
    def _fmt_size(n):
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024.0:
                return f"{n:.1f} {unit}"
            n /= 1024.0
        return f"{n:.1f} TB"

    def _copy_install_path(self):
        QApplication.clipboard().setText(self.loc_label.text())

    def _open_install_folder(self):
        subprocess.Popen(["xdg-open", self.loc_label.text()],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _relocate_blocked(self):
        if self.ui_state.get("launch_active") or _mc_running():
            self.warn_box("Minecraft is running",
                          "Close Minecraft first before changing the game files location.")
            return True
        if self.ui_state.get("busy"):
            self.warn_box("Operation in progress",
                          "Wait for the current preparation task to finish before "
                          "changing the game files location.")
            return True
        return False

    def _do_browse_location(self):
        if self._relocate_blocked():
            return
        if not is_relocation_allowed():
            self.error_box("Relocation disabled",
                "BOL_HOME is set in the environment. The location cannot be "
                "changed via the GUI.")
            return
        chosen = QFileDialog.getExistingDirectory(self, "Choose a folder for BedrockOnLinux's files")
        if not chosen:
            return
        old_dir = Path(get_install_location())
        new_dir = Path(chosen).expanduser()
        if paths_overlap(old_dir, new_dir):
            if old_dir.resolve() == new_dir.resolve():
                return
            self.error_box("Invalid location",
                "The new location can't be inside the current location "
                "(or the other way around). Choose a separate folder.")
            return

        warning_msg = (
            "Game Location Change\n\n"
            f"Current location: {old_dir}\nNew location: {new_dir}\n\n"
            "Your worlds, saves, settings, and login tokens will be moved.\n"
            "The game engine will be re-downloaded for compatibility.\n\n"
            "Proceed with relocation?")
        if not self.question_box("Confirm Relocation", warning_msg):
            return

        existing_data = any((new_dir / item).exists() for item in DIRS_TO_MOVE + FILES_TO_MOVE)
        if existing_data and not self.question_box(
                "Existing data detected",
                "The new location already contains user data. Proceeding will "
                "overwrite matching folders (backed up with .old). Continue?"):
            return

        total_size = 0
        for sub in DIRS_TO_MOVE:
            src = old_dir / sub
            if src.exists() and not src.is_symlink():
                total_size += sum(f.stat().st_size for f in src.rglob("*") if f.is_file())
        for fname in FILES_TO_MOVE:
            src = old_dir / fname
            if src.exists() and src.is_file():
                total_size += src.stat().st_size

        new_path = new_dir if new_dir.exists() else new_dir.parent
        try:
            free_space = shutil.disk_usage(new_path).free
        except Exception as e:
            self.error_box("Could not check free space", str(e))
            return
        if total_size > free_space:
            self.error_box("Not enough free space",
                f"The new location has {self._fmt_size(free_space)} free, but "
                f"you need {self._fmt_size(total_size)}.")
            return

        self.ui_state["busy"] = True
        self.loc_status_label.setText("Moving user data…")

        def work():
            with prefix_operation_lock("relocate user data"):
                migrate_data(old_dir, new_dir)

        w = Worker(work)

        def ok(_r):
            self.ui_state["busy"] = False
            self.loc_status_label.setText("")
            self.loc_label.setText(str(new_dir))
            self._refresh_free_space()
            self.info_box("Relocation Successful",
                "User data moved successfully. The engine will be re-downloaded "
                "on the next start. The launcher will now restart.")
            self.relaunch_app()

        def fail(msg):
            self.ui_state["busy"] = False
            self.loc_status_label.setText("")
            self.error_box("Relocation Error", f"Could not relocate user data:\n{msg}")

        w.done.connect(ok)
        w.failed.connect(fail)
        self._start_worker("relocate", w)

    def _do_reset_location(self):
        if self._relocate_blocked():
            return
        if not is_relocation_allowed():
            self.error_box("Relocation disabled",
                "BOL_HOME is set in the environment. The location cannot be "
                "reset via the GUI.")
            return
        if get_install_location() == default_install_location():
            return
        if not self.question_box("Reset location",
                f"Reset to the default location ({default_install_location()})?\n\n"
                "This only clears the saved preference — it does not move or "
                "delete any files. Restart required."):
            return
        clear_install_location()
        self.loc_label.setText(default_install_location())
        self._refresh_free_space()
        self.info_box("Reset Complete", "Location reset to default. The launcher will now restart.")
        self.relaunch_app()

    def _build_tools_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        content = card_section(v, "Content")
        content.addWidget(tool_row("Import content (.mcpack / .mcworld / .mcaddon / .mcskin)…",
                               self._do_import,
                               tip="Add worlds, resource/behaviour packs, add-ons or "
                                   "skins to Minecraft."))
        content.addWidget(tool_row("Inject a client DLL…", self._do_inject,
                               tip="Load a client-side .dll into the running game. "
                                   "Native / AppImage only."))
        content.addWidget(tool_row("Open Minecraft folder",
                               lambda: subprocess.Popen(["xdg-open", str(game_content_dir())],
                                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
                               tip="Open the folder holding your worlds, templates "
                                   "and screenshots in your file manager."))

        shortcuts = card_section(v, "Shortcuts")
        shortcuts.addWidget(tool_row("Create direct launch shortcut (skips this window)…",
                                 self._do_play_shortcut,
                                 tip="Make a desktop/Steam shortcut that starts Minecraft "
                                     "straight away."))
        shortcuts.addWidget(tool_row("Create isolated Xbox account shortcut…",
                                 self._do_create_profile_shortcut,
                                 tip="Make a new profile with its own Xbox login, Wine "
                                     "prefix and worlds."))

        maintenance = card_section(v, "Maintenance")
        try:
            ack_status = gpu_crash_acknowledgement_status()
        except Exception:
            ack_status = None
        if ack_status and ack_status.can_acknowledge:
            self.gpu_ack_btn = tool_row("Acknowledge previous GPU incident…",
                lambda: self._offer_gpu_ack(ack_status) and self.gpu_ack_btn.hide(),
                danger=True,
                tip="Confirm the previous graphics-driver incident has been "
                    "checked, so PLAY is unblocked again.")
            maintenance.addWidget(self.gpu_ack_btn)
        maintenance.addWidget(tool_row("Open logs folder",
                                   lambda: subprocess.Popen(["xdg-open", str(LOGS)],
                                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
                                   tip="Open the folder with launch and activity logs, "
                                       "useful for bug reports."))
        maintenance.addWidget(tool_row("Repair (reset Wine prefix)",
                                   lambda: threading.Thread(target=reset_prefix, daemon=True).start(),
                                   tip="Reset the Wine prefix Minecraft runs in. Fixes most "
                                       "'won't start' problems; worlds and settings are kept."))
        maintenance.addWidget(tool_row("Force stop Minecraft", kill_wine, danger=True,
                                   tip="Immediately terminate Minecraft and any Wine "
                                       "processes for this profile."))

        self.tools_status_label = QLabel("")
        self.tools_status_label.setStyleSheet(f"color:{self.theme.gold};")
        v.addWidget(self.tools_status_label)
        v.addStretch(1)
        return w

    def _do_import(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Import Minecraft content", "",
            "Minecraft content (*.mcpack *.mcaddon *.mcworld *.mctemplate *.mcskin);;All files (*.*)")
        if not files:
            return
        self.tools_status_label.setText("Importing…")

        def work():
            done, errs = [], []
            for f in files:
                try:
                    done.extend(import_content(f))
                except Exception as e:
                    errs.append(f"{Path(f).name}: {e}")
            return done, errs

        def finished(result):
            done, errs = result
            self.tools_status_label.setText("")
            msg = f"Imported {len(done)} item(s)." if done else "Nothing imported."
            if errs:
                msg += "\n\nProblems:\n• " + "\n• ".join(errs)
            if _mc_running():
                msg += "\n\nMinecraft is running — restart it to see the new content."
            self.info_box("Import", msg)

        def failed(message):
            self.tools_status_label.setText("")
            self.error_box("Import", f"Could not import:\n{message}")

        w = Worker(work)
        w.done.connect(finished)
        w.failed.connect(failed)
        self._start_worker("import", w)

    def _do_inject(self):
        if not _mc_running():
            self.warn_box("DLL injector",
                "Start Minecraft first and wait for the main menu, then inject.")
            return
        last = self.settings.get("injector_dll") or ""
        dll, _ = QFileDialog.getOpenFileName(self, "Choose a client .dll to inject",
                                              str(Path(last).parent) if last else "",
                                              "Client DLL (*.dll);;All files (*.*)")
        if not dll:
            return
        self.tools_status_label.setText("Injecting…")

        def work():
            return run_injector(dll)

        def finished(name):
            # Written here rather than in work(): _save_setting reloads and
            # reassigns self.settings, which the GUI thread reads.
            self._save_setting("injector_dll", dll)
            self.tools_status_label.setText("")
            self.info_box("DLL injector", f"Injected {name} into Minecraft. ✓\n\n"
                           "(Native / AppImage only — not inside the Flatpak sandbox.)")

        def failed(msg):
            self.tools_status_label.setText("")
            self.error_box("DLL injector", f"Couldn't inject:\n{msg}")

        w = Worker(work)
        w.done.connect(finished)
        w.failed.connect(failed)
        self._start_worker("inject", w)

    def _do_create_profile_shortcut(self):
        name, ok = QInputDialog.getText(self, "Create account profile",
            "Profile name (each profile has its own Xbox login, prefix and worlds):")
        if not ok or not name:
            return
        try:
            require_profile_shortcuts_supported()
            profile = create_profile(name)
            shortcut = write_profile_shortcut(name, profile_dir=profile)
            command = profile_launch_command(profile)
        except Exception as exc:
            self.error_box("Account profile", str(exc))
            return
        self.info_box("Account profile created",
            f"Created:\n{profile}\n\nDesktop shortcut:\n{shortcut}\n\n"
            "Add that shortcut as a non-Steam game for the matching Steam "
            f"user.\n\nDirect command:\n{command}")

    def _do_play_shortcut(self):
        try:
            require_shortcuts_supported()
            shortcut = write_play_shortcut()
            command = play_launch_command()
            pending = direct_launch_readiness()
        except Exception as exc:
            self.error_box("Direct launch shortcut", str(exc))
            return
        message = (f"Created:\n{shortcut}\n\nIt starts Minecraft straight away, with "
                   "no launcher window. Add it to Steam with 'Add a Non-Steam Game'.\n\n"
                   f"Direct command:\n{command}")
        if pending:
            message += "\n\nStill to do in the launcher:\n• " + "\n• ".join(pending)
        self.info_box("Direct launch shortcut", message)

    # ------------------------------------------------------------ changelog page
    def _build_changelog(self) -> QWidget:
        page = QFrame(); page.setObjectName("Card")
        outer = QVBoxLayout(page)
        outer.setContentsMargins(20, 18, 20, 18)
        head = QHBoxLayout()
        t = QLabel("Changelog"); t.setObjectName("Title")
        head.addWidget(t)
        head.addStretch(1)
        head.addWidget(btn("← Back", self.toggle_changelog, kind="flat", w=76, h=28))
        outer.addLayout(head)

        self.changelog_tabs = QTabWidget()
        outer.addWidget(self.changelog_tabs, 1)
        self.game_changelog_view = QTextBrowser()
        self.game_changelog_view.setOpenExternalLinks(True)
        self.launcher_changelog_view = QTextBrowser()
        self.launcher_changelog_view.setOpenExternalLinks(True)
        self.changelog_tabs.addTab(self.game_changelog_view, "Game")
        self.changelog_tabs.addTab(self.launcher_changelog_view, "Launcher")
        return page

    def load_changelogs(self, force=False):
        if self._changelog_loaded and not force:
            return
        self._changelog_loaded = True

        from .config import SELF_REPO
        loading = self._wrap_changelog_html("<i class='empty'>Loading…</i>")
        self.game_changelog_view.setHtml(loading)
        self.launcher_changelog_view.setHtml(loading)

        def error_html(e):
            return self._wrap_changelog_html(
                f"<b>Could not load changelog.</b><div class='release-date' "
                f"style='text-transform:none;margin-top:6px;'>{html.escape(e)}</div>")

        gw = Worker(lambda: mc_releases(fetch_all=False))
        gw.done.connect(lambda data: _alive(self) and _alive(self.game_changelog_view)
                         and self.game_changelog_view.setHtml(self._render_game_changelog_html(data)))
        gw.failed.connect(lambda e: _alive(self) and _alive(self.game_changelog_view)
                           and self.game_changelog_view.setHtml(error_html(e)))
        self._start_worker("changelog-game", gw)

        lw = Worker(lambda: gh_releases(SELF_REPO))
        lw.done.connect(lambda data: _alive(self) and _alive(self.launcher_changelog_view)
                         and self.launcher_changelog_view.setHtml(self._render_launcher_changelog_html(data)))
        lw.failed.connect(lambda e: _alive(self) and _alive(self.launcher_changelog_view)
                           and self.launcher_changelog_view.setHtml(error_html(e)))
        self._start_worker("changelog-launcher", lw)

    def _changelog_css(self) -> str:
        """Shared typography for both changelog tabs."""
        t = self.theme
        return f"""
        body {{
            font-family: -apple-system, "Segoe UI", "Inter", sans-serif;
            font-size: 13.5px;
            line-height: 1.55;
            color: {t.fg};
        }}
        h2.release-title {{
            font-size: 17px;
            font-weight: 700;
            color: {t.accent};
            margin: 0 0 2px 0;
        }}
        h2.release-title a {{ color: {t.accent}; text-decoration: none; }}
        div.release-date {{
            font-size: 11.5px;
            font-weight: 600;
            letter-spacing: 0.02em;
            text-transform: uppercase;
            color: {t.sub};
            margin: 0 0 10px 0;
        }}
        div.release-body p {{ margin: 0 0 8px 0; }}
        div.release-body h1, div.release-body h2, div.release-body h3 {{
            font-size: 14px; font-weight: 700; margin: 12px 0 4px 0; color: {t.fg};
        }}
        div.release-body li {{ margin: 0 0 4px 18px; }}
        div.release-body code {{
            background: {t.card2}; color: {t.accent};
            padding: 1px 5px; border-radius: 4px; font-family: monospace;
        }}
        div.release-body blockquote {{
            margin: 6px 0; padding: 4px 12px;
            border-left: 3px solid {t.accent};
            color: {t.sub}; background: {t.card2};
        }}
        div.release-body a {{ color: {t.accent}; }}
        hr {{
            border: none; border-top: 1px solid {t.border};
            margin: 18px 0;
        }}
        i.empty {{ color: {t.sub}; }}
        """

    def _wrap_changelog_html(self, body_html: str) -> str:
        return f"<html><head><style>{self._changelog_css()}</style></head><body>{body_html}</body></html>"

    def _md_to_html(self, text: str) -> str:
        """Small, dependency-free Markdown → HTML used for release bodies."""
        text = html.escape(text or "")
        text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
        text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
        text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
        lines = []
        in_list = False
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("#"):
                if in_list:
                    lines.append("</ul>"); in_list = False
                n = min(3, len(stripped) - len(stripped.lstrip("#")))
                lines.append(f"<h{n+1}>{stripped.lstrip('#').strip()}</h{n+1}>")
            elif stripped.startswith(("* ", "- ", "+ ")):
                if not in_list:
                    lines.append("<ul>"); in_list = True
                lines.append(f"<li>{stripped[2:]}</li>")
            elif stripped.startswith(">"):
                if in_list:
                    lines.append("</ul>"); in_list = False
                lines.append(f"<blockquote>{stripped[1:].strip()}</blockquote>")
            elif stripped == "":
                if in_list:
                    lines.append("</ul>"); in_list = False
            else:
                if in_list:
                    lines.append("</ul>"); in_list = False
                lines.append(f"<p>{stripped}</p>")
        if in_list:
            lines.append("</ul>")
        return "\n".join(lines)

    def _render_launcher_changelog_html(self, rels) -> str:
        if not rels:
            return self._wrap_changelog_html("<i class='empty'>No releases found.</i>")
        parts = []
        for rel in rels:
            tag = rel.get("tag_name", "Unknown")
            name = rel.get("name")
            date = (rel.get("published_at") or "").split("T")[0]
            body = (rel.get("body") or "").strip()
            url = rel.get("html_url")
            title = f"{tag} — {name}" if name and name != tag else tag
            title_html = f'<a href="{url}">{html.escape(title)}</a>' if url else html.escape(title)
            parts.append(f"<h2 class='release-title'>{title_html}</h2>")
            parts.append(f"<div class='release-date'>{date}</div>")
            if body:
                parts.append(f"<div class='release-body'>{self._md_to_html(body)}</div>")
            parts.append("<hr>")
        return self._wrap_changelog_html("".join(parts))

    def _render_game_changelog_html(self, data) -> str:
        if not _alive(self) or not _alive(self.ver_label):
            return ""
        lab = self.ver_label.text()
        ui_wants_beta = "BETA" in lab if lab else False
        articles = []
        for art in data.get("articles", []):
            title = art.get("title", "Unknown Release")
            if not ("bedrock" in title.lower() or "beta" in title.lower() or "preview" in title.lower()):
                continue
            is_beta = "beta" in title.lower() or "preview" in title.lower()
            if is_beta == ui_wants_beta:
                articles.append(art)
        articles = articles[:40]

        if not articles:
            return self._wrap_changelog_html("<i class='empty'>No releases found.</i>")

        parts = []
        for art in articles:
            title = art.get("title", "Unknown Release")
            is_beta = "beta" in title.lower() or "preview" in title.lower()
            title = format_display_version(title, is_beta)
            date = (art.get("updated_at") or "").split("T")[0]
            body = art.get("body") or ""
            url = art.get("html_url")
            title_html = f'<a href="{url}">{html.escape(title)}</a>' if url else html.escape(title)
            parts.append(f"<h2 class='release-title'>{title_html}</h2>")
            parts.append(f"<div class='release-date'>{date}</div>")
            parts.append(f"<div class='release-body'>{body}</div>")  # already HTML from the API
            parts.append("<hr>")
        return self._wrap_changelog_html("".join(parts))

    # ------------------------------------------------------------ self-update
    def check_for_update_async(self):
        w = Worker(check_for_update)
        w.done.connect(lambda rel: rel and _alive(self) and self._show_update_banner(rel))
        self._start_worker("update-check", w)

    def _show_update_banner(self, rel):
        if not _alive(self) or self.centralWidget() is None:
            return
        bar = QFrame(); bar.setObjectName("CardFlat")
        h = QHBoxLayout(bar)
        lab = QLabel(f"⟳  Update available — v{rel['version']}  (you have {VERSION})")
        lab.setStyleSheet(f"color:{self.theme.blue}; font-weight:700;")
        h.addWidget(lab)
        h.addStretch(1)
        h.addWidget(btn("Later", lambda: bar.setParent(None), kind="flat", w=64, h=30))
        h.addWidget(btn("Update now", lambda: self._run_update(rel, bar), kind="primary", w=112, h=30))
        self.centralWidget().layout().insertWidget(1, bar)

    def _run_update(self, rel, banner):
        banner.setParent(None)
        self.set_status(f"Updating to v{rel['version']}…")
        self._show_bar_busy()

        w = Worker(self_update, rel)
        w.progress.connect(self.set_progress)

        def done(result):
            state, msg = result
            self.end_progress()
            self.set_status(
                msg, self.theme.green if state == "ok"
                else (self.theme.red if state == "error" else None))
            if state == "ok":
                self._restart_prompt()

        w.done.connect(done)
        self._start_worker("update", w)

    def _restart_prompt(self):
        if self.question_box("Update installed", "Restart now to run the new version?"):
            self.relaunch_app()

    def relaunch_app(self):
        self.na.stop()
        try:
            if os.environ.get("APPIMAGE"):
                os.execv(os.environ["APPIMAGE"], [os.environ["APPIMAGE"], "gui"])
            main_spec = getattr(sys.modules.get("__main__"), "__spec__", None)
            if main_spec and main_spec.name:
                os.execv(sys.executable, [sys.executable, "-m", "bol", "gui"])
            tgt = os.path.realpath(sys.argv[0] or __file__)
            os.execv(sys.executable, [sys.executable, tgt, "gui"])
        except Exception:
            QApplication.instance().quit()

    # ------------------------------------------------------------ message boxes
    def _box(self, icon, title, message) -> QMessageBox:
        box = QMessageBox(self)
        box.setIcon(icon)
        box.setWindowTitle(title)
        box.setText(message)
        box.setStyleSheet(self.theme.qss())
        return box

    def info_box(self, title, message):
        self._box(QMessageBox.Information, title, message).exec()

    def warn_box(self, title, message):
        self._box(QMessageBox.Warning, title, message).exec()

    def error_box(self, title, message):
        self._box(QMessageBox.Critical, title, message).exec()

    def question_box(self, title, message) -> bool:
        box = self._box(QMessageBox.Question, title, message)
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        return box.exec() == QMessageBox.Yes

    # ------------------------------------------------------------ close handling
    def closeEvent(self, event):
        if self._force_close:
            event.accept()
            return
        if self.ui_state.get("launch_active"):
            self.warn_box("Minecraft is running",
                "Close Minecraft first and wait for the launcher to report that "
                "it closed. To abort it, use Settings → Tools → Force stop Minecraft.")
            event.ignore()
            return
        if self.ui_state.get("busy"):
            self.warn_box("Operation in progress",
                "Wait for the current preparation task to finish before closing "
                "the launcher.")
            event.ignore()
            return
        self.na.stop()
        self._watch_for_stray_clicks(False)
        event.accept()
        # The app runs with setQuitOnLastWindowClosed(False) so that
        # "close the launcher when Minecraft starts" can drop the window while
        # the launch thread keeps supervising the game. That makes quitting an
        # explicit act: without this the event loop outlives the window and the
        # process stays resident forever. `_force_close` returns above, so the
        # close-on-launch path still leaves the loop running for LaunchWorker.
        QApplication.instance().quit()

    def eventFilter(self, watched, event):
        if (event.type() == QEvent.MouseButtonPress
                and getattr(self, "_acct_confirm", False)
                and watched is not self.acct_btn
                and not (isinstance(watched, QWidget)
                         and self.acct_btn.isAncestorOf(watched))):
            self._disarm_account_confirm()
        return super().eventFilter(watched, event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not isinstance(
                QApplication.focusWidget(), (QLineEdit, QPlainTextEdit, QTextEdit)):
            self.do_play()
        else:
            super().keyPressEvent(event)


# ======================================================================
# Profile manager dialog
# ======================================================================

class ProfileManagerDialog(QDialog):
    def __init__(self, main: MainWindow):
        super().__init__(main)
        self.main = main
        self.setWindowTitle("Manage Profiles")
        self.resize(620, 440)
        self.setStyleSheet(main.theme.qss())

        v = QVBoxLayout(self)
        title = QLabel("Account Profiles"); title.setObjectName("Title")
        v.addWidget(title)
        desc = QLabel("Each profile maintains an isolated Xbox sign-in, Wine "
                       "prefix, worlds, and settings.")
        desc.setObjectName("Sub")
        desc.setWordWrap(True)
        v.addWidget(desc)

        area = QScrollArea(); area.setWidgetResizable(True)
        self.list_widget = QWidget()
        self.list_layout = QVBoxLayout(self.list_widget)
        area.setWidget(self.list_widget)
        v.addWidget(area, 1)

        self.refresh()

    def refresh(self):
        while self.list_layout.count():
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        active_info = current_profile_info()
        active_path = active_info.get("path")

        def add_row(name, path, subtitle, is_active):
            row = QFrame(); row.setObjectName("CardFlat")
            h = QHBoxLayout(row)
            left = QVBoxLayout()
            nlab = QLabel(name); nlab.setStyleSheet("font-weight:700;")
            left.addWidget(nlab)
            slab = QLabel(subtitle); slab.setObjectName("Muted")
            left.addWidget(slab)
            h.addLayout(left, 1)

            h.addWidget(btn("New Window", lambda: open_profile_window(path), kind="ghost", w=90, h=28))
            if path is not None:
                h.addWidget(btn("Folder", lambda: subprocess.Popen(["xdg-open", str(path)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
                                kind="ghost", w=54, h=28))
                h.addWidget(btn("Rename", lambda: self._rename(name, is_active), kind="ghost", w=60, h=28))
                h.addWidget(btn("Delete", lambda: self._delete(name, is_active), kind="danger", w=60, h=28))
            if is_active:
                lab = QLabel("Active"); lab.setStyleSheet(f"color:{self.main.theme.green}; font-weight:700;")
                h.addWidget(lab)
            else:
                h.addWidget(btn("Switch", lambda: self._switch(path), kind="ghost", w=60, h=28))
            self.list_layout.addWidget(row)

        add_row("Default", None, "Main installation root", active_path is None)
        for p in list_profiles():
            path = p.get("path")
            is_active = active_path is not None and Path(active_path).resolve() == Path(path).resolve()
            add_row(p.get("name", ""), path, f"profiles/{p.get('slug', '')}", is_active)
        self.list_layout.addStretch(1)

    def _switch(self, path):
        if self.main._switch_profile_target(path):
            self.accept()

    def _rename(self, name, is_active):
        if is_active and (self.main.ui_state.get("launch_active") or self.main.ui_state.get("busy")):
            self.main.warn_box("Rename Profile",
                "Cannot rename the active profile while Minecraft or a task "
                "is running in this window.")
            return
        new_name, ok = QInputDialog.getText(self, "Rename Profile", f"New name for '{name}':")
        if not ok or not new_name.strip() or new_name.strip() == name:
            return
        try:
            new_dir = rename_profile(name, new_name.strip())
            active_path = current_profile_info().get("path")
            if is_active and active_path is not None and Path(new_dir).resolve() != Path(active_path).resolve():
                self._switch(new_dir)
                return
            self.refresh()
        except Exception as exc:
            self.main.error_box("Rename Profile", str(exc))

    def _delete(self, name, is_active):
        if is_active:
            self.main.warn_box("Delete Profile", "Cannot delete the currently active profile.")
            return
        if not self.main.question_box("Delete Profile",
                f"Are you sure you want to delete profile '{name}'?\n\n"
                "This will permanently remove its worlds, settings, and player data."):
            return
        try:
            delete_profile(name)
            self.refresh()
        except Exception as exc:
            self.main.error_box("Delete Profile", str(exc))


# ======================================================================
# GPU safety incident acknowledgement (module-level, not a MainWindow method)
# ======================================================================

def _gpu_incident_safety_instruction(status):
    """Plain-text instruction that must be shown before an incident marker
    can be acknowledged. Module-level so it, and the confirmation flow
    below, are importable/testable without a QApplication."""
    return (
        "Continue only after repairing/updating the graphics driver and rebooting."
        if status.previous_boot_fault else
        "No fatal driver event was detected for this marker. Continue only "
        "after checking why the previous session or machine stopped.")


def _offer_gpu_incident_acknowledgement(
        box, parent, status, prefix="",
        title="Acknowledge previous GPU incident"):
    """Confirm + acknowledge a GPU safety incident marker. `box` is duck-typed
    on askyesno/showinfo/showerror(title, message, parent=None), so this
    needs no real dialog widget to run or to test. The eligibility decision
    itself is never made here: acknowledge_gpu_crash() re-checks it under the
    launch lock, and a refusal is reported from its live status rather than
    the one passed in, in case it changed in the meantime."""
    instruction = _gpu_incident_safety_instruction(status)
    message = prefix + status.message + "\n\n" + instruction + " Acknowledge now?"
    if not box.askyesno(title, message, parent=parent):
        return False
    if acknowledge_gpu_crash():
        box.showinfo(
            "GPU safety",
            "The previous-boot incident was acknowledged. "
            "PLAY will still run all current graphics safety checks.",
            parent=parent)
        return True
    box.showerror("GPU safety", gpu_crash_acknowledgement_status().message,
                   parent=parent)
    return False


class _MainWindowMessageBoxAdapter:
    """Adapts MainWindow's Qt-backed info_box/error_box/question_box onto the
    askyesno/showinfo/showerror shape _offer_gpu_incident_acknowledgement
    expects. `parent` is accepted for API compatibility but unused: the
    underlying QMessageBox is already parented to the window itself."""

    def __init__(self, window):
        self._window = window

    def askyesno(self, title, message, parent=None):
        return self._window.question_box(title, message)

    def showinfo(self, title, message, parent=None):
        self._window.info_box(title, message)

    def showerror(self, title, message, parent=None):
        self._window.error_box(title, message)


# ======================================================================
# Entry point
# ======================================================================

def gui():
    """Launch the PySide6 GUI."""
    from .deps import ensure_gui_deps
    ensure_gui_deps()
    _resolve_gui_display(os.environ)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(PRETTY)
    app.setStyle("Fusion")
    # "Close the launcher when Minecraft starts" closes the window while the
    # launch thread is still supervising the game; the process itself exits
    # once that thread finishes (see MainWindow._close_for_game).
    app.setQuitOnLastWindowClosed(False)

    try:
        window = MainWindow()
    except Exception as e:
        _desktop_error(f"GUI failed to start ({e}). Use the command line instead.")
        return

    window.show()
    app.exec()

"""pytest configuration shared by the whole suite.

Deliberately thin. An earlier version installed a session-wide autouse
fixture that patched bol.gui.NativeAuth, which imported bol.gui before any
test ran -- so every test in the repository, including the ones about
prefixes, launching and packaging, could only run where PySide6 was
installed. The GUI tests build their own window through tests/guiharness.py
instead, and nothing else has to know the GUI exists.
"""
# SPDX-License-Identifier: MIT

import os
import sys
from pathlib import Path

# Ensure bol (and tests.*) can be imported however pytest was invoked.
sys.path.insert(0, str(Path(__file__).parent.parent))

# Qt needs a platform plugin at import time and CI has no display. Offscreen
# is set here rather than only in the workflow so a local run behaves the
# same. DISPLAY is deliberately left alone: the display-resolution tests are
# about what the launcher does with the real environment, and a fake value
# planted here would mask that.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def pytest_configure(config):
    config.addinivalue_line("markers", "gui: mark test as a GUI test")
    config.addinivalue_line(
        "markers",
        "requires_display: mark test as requiring an X11/Wayland display")

"""conftest.py — pytest configuration and shared fixtures for BedrockOnLinux tests."""
# SPDX-License-Identifier: MIT

import os
import sys
from pathlib import Path

# Ensure bol module can be imported in tests
sys.path.insert(0, str(Path(__file__).parent.parent))

# Set headless mode for Qt (required in CI)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("DISPLAY", ":99")

import pytest
from unittest import mock


def pytest_configure(config):
    """Configure pytest before running tests."""
    # Register custom markers
    config.addinivalue_line(
        "markers", "gui: mark test as a GUI test"
    )
    config.addinivalue_line(
        "markers", "requires_display: mark test as requiring an X11/Wayland display"
    )


@pytest.fixture(scope="session", autouse=True)
def setup_test_environment():
    """Set up test environment before any tests run."""
    # Mock expensive initialization that's not needed for unit tests
    with mock.patch("bol.gui.NativeAuth"):
        with mock.patch("bol.log._LOG_SINK"):
            yield


@pytest.fixture
def mock_settings():
    """Provide mock settings for tests."""
    return {
        "light_theme": False,
        "ui_is_beta": False,
        "show_betas": False,
        "show_changelog_on_startup": False,
        "ray_tracing": True,
        "limit_frame_rate": True,
        "confine_cursor": False,
        "close_on_launch": False,
        "diagnostics": False,
    }


@pytest.fixture
def mock_profiles():
    """Provide mock profile data for tests."""
    return [
        {
            "name": "Test Profile 1",
            "path": "/tmp/profile1",
            "slug": "test-profile-1"
        },
        {
            "name": "Test Profile 2",
            "path": "/tmp/profile2",
            "slug": "test-profile-2"
        },
    ]


@pytest.fixture
def mock_versions():
    """Provide mock version data for tests."""
    return [
        {
            "tag": "1.0.0",
            "beta": False,
            "edition": {"id": "release", "name": "Release"},
            "installed": True,
        },
        {
            "tag": "1.1.0-beta",
            "beta": True,
            "edition": {"id": "preview", "name": "Preview", "beta": True},
            "installed": False,
        },
    ]

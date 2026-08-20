"""bol.config — constants, paths, repos and URLs (no logic, no side effects)."""
# SPDX-License-Identifier: MIT

import os
from pathlib import Path

APP = "bedrock-on-linux"
PRETTY = "BedrockOnLinux"
VERSION = "2.2.1"
# Published Flatpak identity; used to print a runnable command for that layout.
FLATPAK_APP_ID = "io.github.wyze3306.BedrockOnLinux"

HOME = Path.home()
XDG_DATA_HOME = Path(
    os.environ.get("XDG_DATA_HOME") or HOME / ".local" / "share"
).expanduser()
XDG_CONFIG_HOME = Path(
    os.environ.get("XDG_CONFIG_HOME") or HOME / ".config"
).expanduser()
DEFAULT_DATA = XDG_DATA_HOME / APP
LEGACY_DATA = HOME / ".local" / "share" / APP

# Resolve relocation before exporting DATA; imported path constants cannot be
# changed afterwards. BOL_HOME takes priority over the persistent pointer.
INSTALL_LOCATION_FILE = XDG_CONFIG_HOME / APP / "install_location"
LEGACY_INSTALL_LOCATION_FILE = HOME / ".config" / APP / "install_location"

_bol_home = os.environ.get("BOL_HOME", "").strip()
if _bol_home:
    _data_path = _bol_home
else:
    _data_path = str(DEFAULT_DATA)
    # Keep the pre-XDG pointer fallback so upgrades retain relocated data.
    _pointer_candidates = (INSTALL_LOCATION_FILE,)
    if LEGACY_INSTALL_LOCATION_FILE != INSTALL_LOCATION_FILE:
        _pointer_candidates += (LEGACY_INSTALL_LOCATION_FILE,)
    for _pointer in _pointer_candidates:
        try:
            if not _pointer.is_file():
                continue
            _custom_home = _pointer.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if _custom_home:
            _data_path = _custom_home
            break

DATA = Path(_data_path)
PROTON_DIR = DATA / "proton"
UMU_DIR = DATA / "umu"
COMPAT = DATA / "compatdata"
PFX = COMPAT / "pfx"
GAMES = DATA / "games"
CONTENT = DATA / "content"
CACHE = DATA / "cache"
LOGS = DATA / "logs"
MSA_DIR = DATA / "msa"
SETTINGS = DATA / "settings.json"

GDK_PROTON_REPO = "Weather-OS/GDK-Proton"
UMU_REPO = "Open-Wine-Components/umu-launcher"
UMU_VERSION = "1.4.3"
UMU_ASSET = "umu-launcher-1.4.3-zipapp.tar"
UMU_ARCHIVE_SHA256 = \
    "3f8fdc033f547afdb3408ea48ad07194769405148dcfa2b2f945b7fb368a33bb"
UMU_RUN_SHA256 = \
    "577181dbff2eccdaa78b411c0fd1aa7fde574028449c3e0e99f508536a76870e"
MINGW_CURL = "https://mirror.msys2.org/mingw/mingw64/mingw-w64-x86_64-curl-8.17.0-1-any.pkg.tar.zst"
CACERT_URL = "https://curl.se/ca/cacert.pem"

# WineGDK reads the refresh token from this registry key and requires its
# hardcoded MSA application ID.
MSA_CLIENT_ID = "0000000048183522"
MSA_SCOPE = "service::user.auth.xboxlive.com::MBI_SSL"
MSA_CONNECT = "https://login.live.com/oauth20_connect.srf"
MSA_TOKEN = "https://login.live.com/oauth20_token.srf"
WINEGDK_REG = r"Software\Wine\WineGDK"

# Exact WineGDK source used for the reviewed native-online engine.
WINEGDK_SOURCE_COMMIT = "75637b674e1f191e65753663c4c0c32bea05ba6e"
GDK_DEPS_URL = "https://github.com/minecraft-linux/mcpelauncher-gdk-dependencies/releases/download/v0.0.0"
GDK_DEPS_DLLS = ("libHttpClient.GDK.dll", "XCurl.dll")
# This OpenSSL XCurl payload avoids Wine secur32 failures against Azure and is
# built reproducibly by scripts/build-openssl-xcurl.sh.
OPENSSL_XCURL_SET = DATA / "xodus-xcurl" / "openssl-set"
OPENSSL_XCURL_REV = "504bb166e4e7"
# Integrity pin for the complete online-login payload.
OPENSSL_XCURL_ARCHIVE_SHA256 = "504bb166e4e737ad81c3ac8e7a917740b28478f69acd89e538c3bf921c29523f"
WINEGDK_OUT = PROTON_DIR / "GDK-Proton-xuser"
# Managed engines are accepted only when their archive and source identity
# match the reviewed pins below.
WINEGDK_PREBUILT_REPO = "Wyze3306/BedrockOnLinux"
# The commit alone does not identify vendored follow-up patches.
WINEGDK_SOURCE_MANIFEST_SHA256 = "0feb01ca058086eccf4f4a0e6895f541547ae89aa0d2ab86f08291224de5ed46"
WINEGDK_BUILD_REV = "wow64-archs-native16"
# native16 carries the ntdll loader patches the Microsoft Store packages need:
# 0007 maps the main image from a descriptor, 0008 from a path so it survives
# the Steam Linux Runtime container. Their game executable stays encrypted on
# disk, so an engine without them cannot start the game -- which is why an
# unset pin here makes _verify_engine_archive() refuse every candidate rather
# than fall back. Produced by the reviewed build-engine.yml run of this branch,
# which refuses any archive that does not reproduce these bytes.
WINEGDK_ARCHIVE_SHA256 = "55f29ad109dbb28e5b4f1fd3b527ff886b75bbd4169f89ac6c7bcdbe503c4ec5"
# Build workflows verify this deterministic intermediate before reusing it.
WINEGDK_PREFIX_SHA256 = "eeb5079fa9736f2d5b71d95d72d64fd56fe51b5be7df220d0a535d2897165dde"

SELF_REPO = WINEGDK_PREBUILT_REPO

# Minecraft is acquired through Xodus (GPL-3.0), which signs in to the user's
# own Microsoft account, obtains the title license and streams the MSIXVC
# package from the official Xbox CDN. It replaced a third-party repository that
# redistributed a DRM-stripped copy of the game. See third_party/xodus/README.md.
XODUS_REPO = "xodus-gaming/xodus"
XODUS_SOURCE_COMMIT = "4615749c6e02cc3b9acce2abbe9916fe8c376f9a"
XODUS_REV = "4615749c6e02"
# Integrity pin for the CI-built xodus-cli archive, produced by the reviewed
# build-xodus.yml run of this branch. The workflow refuses any archive that
# does not reproduce these bytes.
XODUS_ARCHIVE_SHA256 = "0cd9bd42d80ccf588a1f974113a7579737b041b0b7bd8e87eb8dc5d37d00c1f6"
XODUS_DIR = DATA / "xodus"
XODUS_BIN = XODUS_DIR / "xodus-cli"
# xodus-cli links wry/tao unconditionally, so libwebkit2gtk-4.1 has to be
# loadable before main() runs -- not only for the sign-in window, but for the
# download and for `xodus-cli run`, which starts every encrypted game. Hosts
# that ship no WebKitGTK and cannot install one (SteamOS and other immutable
# images, issue #184) get this runtime instead: the closure of that stack,
# built by build-xodus.yml from the same pinned snapshot as xodus-cli.
XODUS_WEBVIEW_DIR = DATA / "xodus-webview"
XODUS_WEBVIEW_REV = "trixie-1"
# Integrity pin for the CI-built runtime, produced by the reviewed
# build-xodus.yml run of this branch. Empty means "never published", and the
# launcher then reports the missing library instead of installing unverified
# bytes -- publish .github/workflows/build-xodus.yml and pin the SHA-256 it
# prints. One line, like every pin here: the build and CI checks read it with
# grep + cut, and a continuation makes them compare the variable name.
XODUS_WEBVIEW_SHA256 = "a9b04506446ba57fe40bae9e731857e681da230ce4db20e6613ae558441a0c6e"
# The compiled-in directory WebKitGTK spawns its helper processes from. Modern
# builds drop the WEBKIT_EXEC_PATH override (it is developer-mode only), so the
# bundled library carries this literal and the launcher rewrites it in place.
XODUS_WEBVIEW_EXEC_DIR = "/usr/lib/x86_64-linux-gnu/webkit2gtk-4.1"
# Xodus keeps its tokens in a file keyring (built with --features
# key-chain-file) instead of a D-Bus secret service, which does not exist in a
# Game Mode session or inside a Flatpak sandbox.
XODUS_KEYRING = HOME / ".xodus-keyring.ron"

# GetBasePackage only ever answers with the current build, but Microsoft's CDN
# keeps the older ones reachable, and MinecraftBedrockArchiver/GdkLinks indexes
# where they live. That index holds no game data: every URL points at
# assets*.xboxlive.com, and Xodus still reads the package's own content id from
# the downloaded header and asks Microsoft for that licence, so the account
# still has to own Minecraft. It only restores the choice of build.
GDK_LINKS_REPO = "MinecraftBedrockArchiver/GdkLinks"
GDK_LINKS_URL = ("https://raw.githubusercontent.com/"
                 "MinecraftBedrockArchiver/GdkLinks/master/urls.json")
# The CDN serves these over plain HTTP only. The payload is AES-XTS encrypted
# and worthless without the licence, so this costs no confidentiality; the
# content id below is checked against every indexed URL so a bad or tampered
# index cannot point the downloader at a different product.
MC_PRODUCTS = (
    {"id": "release", "product": "9NBLGGH2JHXJ", "channel": "release",
     "content_id": "7792d9ce-355a-493c-afbd-768f4a77c3b0",
     "name": "Minecraft for Windows", "beta": False},
    {"id": "preview", "product": "9P5X4QVLC2XR", "channel": "preview",
     "content_id": "98bd2335-9b01-4e4c-bd05-ccc01614078b",
     "name": "Minecraft Preview for Windows", "beta": True},
)


def _legacy_install_location_file() -> Path:
    return HOME / ".config" / APP / "install_location"


def get_install_location() -> str:
    """Where the app's data directory currently resolves to."""
    return str(DATA)

def default_install_location() -> str:
    """The location used when no custom install location is set."""
    return str(DEFAULT_DATA)

def set_install_location(path) -> None:
    """Persist a custom data-directory location for future runs.

    Raises RuntimeError if BOL_HOME is set externally (relocation disabled).
    """
    if os.environ.get("BOL_HOME", "").strip():
        raise RuntimeError("Cannot change location when BOL_HOME is set externally")
    INSTALL_LOCATION_FILE.parent.mkdir(parents=True, exist_ok=True)
    INSTALL_LOCATION_FILE.write_text(
        str(Path(path).expanduser()), encoding="utf-8")
    legacy = _legacy_install_location_file()
    if legacy != INSTALL_LOCATION_FILE:
        legacy.unlink(missing_ok=True)

def clear_install_location() -> None:
    """Revert to the default location.

    Raises RuntimeError if BOL_HOME is set externally (relocation disabled).
    """
    if os.environ.get("BOL_HOME", "").strip():
        raise RuntimeError("Cannot change location when BOL_HOME is set externally")
    INSTALL_LOCATION_FILE.unlink(missing_ok=True)
    legacy = _legacy_install_location_file()
    if legacy != INSTALL_LOCATION_FILE:
        legacy.unlink(missing_ok=True)

def is_relocation_allowed() -> bool:
    """Return True if the data directory can be relocated via the GUI."""
    return not bool(os.environ.get("BOL_HOME", "").strip())

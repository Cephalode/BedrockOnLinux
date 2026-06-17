"""bol.doctor — environment health checks."""
# SPDX-License-Identifier: MIT

import shutil
import sys

from . import deps
from .config import PRETTY, VERSION
from .log import info, ok, warn
from .platform import IS_MAC, pm_hint


def doctor():
    info(f"{PRETTY} {VERSION} — system check")
    hint = pm_hint()
    miss = []
    print(f"  {'python3':12} : {sys.version.split()[0]}")
    if IS_MAC:
        # macOS runs the game through a native Wine (Game Porting Toolkit /
        # CrossOver / Wine), not GDK-Proton — check one is present.
        from . import winemac
        backend, wine = winemac.detect_wine()
        print(f"  {'wine':12} : "
              f"{backend + ' (' + str(wine) + ')' if wine else 'MANQUANT'}")
        if not wine:
            miss.append("apple/apple/game-porting-toolkit")
        tools = (("tar", "tar"), ("curl", "curl"))
        tk_pkg, cr_pkg = "python-tk", "cryptography"
    else:
        tools = (("tar", "tar"), ("curl", "curl"), ("unzstd", "zstd"))
        tk_pkg, cr_pkg = "python3-tk", "python3-cryptography"
    for tool, pkg in tools:
        have = shutil.which(tool)
        print(f"  {tool:12} : {'OK' if have else 'MANQUANT'}")
        if not have and not (tool == "curl" and shutil.which("wget")):
            miss.append(pkg)
    # tkinter (GUI) — probe by import spec so a missing Tk doesn't raise here
    tk_ok = deps.have("tkinter")
    print(f"  {'tkinter':12} : {'OK (GUI)' if tk_ok else 'MANQUANT (GUI)'}")
    if not tk_ok:
        miss.append(tk_pkg)
    # cryptography (native Microsoft login) — see bol.deps
    cr_ok = deps.have("cryptography")
    print(f"  {'cryptography':12} : "
          f"{'OK (login)' if cr_ok else 'MANQUANT (login)'}")
    if not cr_ok:
        miss.append(cr_pkg)
    if miss:
        warn("To install: " + hint.format(" ".join(sorted(set(miss)))))
        return False
    ok("System ready.")
    return True

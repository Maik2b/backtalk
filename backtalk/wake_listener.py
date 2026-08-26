# backtalk: talk to your Claude Code agent out loud.
# Copyright (C) 2026 Jared Rhodenizer
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published
# by the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
#
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The cold-start listener — "Hey Jarvis" with NOTHING running yet.

A separate, always-on process from backtalk itself: backtalk's own
wake-word gate (wake.py, mic_mode "wake") only exists WHILE backtalk is
already running, so it can never be what wakes the stack from a cold
boot — nothing is listening yet at that point. This script is that
"nothing": tiny, no LLM, no TTS, just the same hey_jarvis detector in a
loop, meant to run in the background from login onward (see
install_autostart.py) or be started by hand.

Loop: wait for the wake word -> check whether backtalk is already up
(is_backtalk_running) -> if not, launch talk.bat (the real daily
launcher: voice, the face as its own Chrome app window, and
jarvis-bridge — see ClaudeBrain/talk.bat, kept outside fullstack-agent
on purpose so "Update Jarvis" never touches it), in its own console so
this script's own window is never adopted as the voice line's window
-> stand down for a while so it isn't competing for the mic with the
backtalk process it just started -> resume listening (in case that
launch failed, or backtalk was later closed).

Windows only, matching wake.py/screen.py/launch.py's platform scope.
"""
import ctypes
import os
import subprocess
import sys
import time

from backtalk import wake
from backtalk.config import CFG, REPO
from backtalk.vlog import log

_LOCK_FILE = os.path.join(CFG["signals_dir"], ".backtalk_pid")
_START_BAT = REPO.parent / "talk.bat"
# After a launch, back off from listening for this long: backtalk's own
# startup (model loads, browser windows) takes a real stretch (the
# logs show ears/mouth landing several seconds apart), and this
# process's mic must not compete with backtalk's for that window.
_COOLDOWN_S = 45


def is_backtalk_running() -> bool:
    """True if a backtalk process is alive right now. Reads the PID
    lock main.py writes for its whole run and removes on clean exit;
    a PID that's no longer alive means a crash left the file behind,
    so that's treated as NOT running rather than trusting the file's
    mere existence."""
    try:
        with open(_LOCK_FILE) as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = ctypes.windll.kernel32.OpenProcess(
        PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


def _launch_stack():
    if not _START_BAT.is_file():
        log(f"[wake-listener] talk.bat not found at {_START_BAT}")
        return
    log("[wake-listener] launching the stack (talk.bat)...")
    # CREATE_NEW_CONSOLE: the new cmd.exe (and everything talk.bat
    # opens under it, including backtalk's own foreground window) gets
    # a real, independent console of its own — so closing THIS
    # listener's window never takes the voice line down with it.
    # DETACHED_PROCESS (no console at all) was tried first and looked
    # right in isolation, but talk.bat's own nested `start "..." cmd
    # /c ...` calls need a console to attach their new windows to;
    # without one those inner launches silently failed and nothing
    # ever appeared on screen. CREATE_NEW_CONSOLE gives the whole
    # chain a console to work with while still being fully separate
    # from this listener's own.
    CREATE_NEW_CONSOLE = 0x00000010
    subprocess.Popen(
        ["cmd", "/c", str(_START_BAT)],
        cwd=str(_START_BAT.parent),
        creationflags=CREATE_NEW_CONSOLE,
        close_fds=True,
    )


def run():
    log("[wake-listener] up — waiting for \"hey jarvis\" "
        "(backtalk not required to be running)")
    while True:
        heard = wake.wait_for_wake()
        if not heard:
            continue
        if is_backtalk_running():
            log("[wake-listener] heard it, but backtalk is already up "
                "— nothing to do")
            time.sleep(_COOLDOWN_S)
            continue
        _launch_stack()
        time.sleep(_COOLDOWN_S)


if __name__ == "__main__":
    if sys.platform != "win32":
        print("[wake-listener] Windows only for now.", flush=True)
        sys.exit(1)
    try:
        run()
    except KeyboardInterrupt:
        print("\n[wake-listener] stopped", flush=True)

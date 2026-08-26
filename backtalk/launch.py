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
"""Named launch macros — "start my study session", by voice.

Windows only, same dependency-free approach as screen.py: each item is
either a URL (opened in Chrome, one process per URL so each lands in
its own window) or a local path (opened with the shell's default
handler, e.g. obsidian:// for the vault). No window-snapping yet —
Chrome/Obsidian remember their last size and position on their own,
which covers the common case without any Win32 juggling.
"""
import subprocess

from backtalk.vlog import log

# Each macro: a name -> ordered list of items to open. An item is a
# URL (http/https/obsidian scheme) opened via the default handler, so
# no hardcoded browser path is needed and whatever the user's default
# browser is gets used.
MACROS = {
    "study_session": [
        "obsidian://open?path=C%3A%5CMy%20AI%5CClaudeBrain",
        "https://www.udemy.com/course/machinelearning/learn/quiz/5962770#overview",
        "https://colab.research.google.com",
    ],
}


def run(name: str) -> bool:
    """Fires every item in the named macro. Best-effort per item — one
    failure (e.g. a site down) never blocks the rest. Returns False
    only if the macro name itself is unknown."""
    items = MACROS.get(name)
    if items is None:
        log(f"[launch] unknown macro: {name!r}")
        return False
    for item in items:
        try:
            subprocess.Popen(["cmd", "/c", "start", "", item],
                             shell=False)
        except OSError as e:
            log(f"[launch] failed to open {item!r}: {e}")
    log(f"[launch] macro {name!r} fired ({len(items)} items)")
    return True


# ai-visualizer's fixed port (server.py binds 127.0.0.1:8790) and
# jarvis-bridge's (127.0.0.1:8791, see ClaudeBrain/jarvis-bridge.py).
# Neither PID is handed back at launch time (talk.bat opens both in
# their own detached consoles), so a "close everything" shutdown finds
# them by the one thing that's always true while they're up:
# something is listening on their port. netstat, not a new dependency
# — same "shell out to a system tool" approach screen.py uses for its
# screenshot.
_FACE_PORT = 8790
_BRIDGE_PORT = 8791
# talk.bat opens the face as a dedicated Chrome APP window
# (--app=http://127.0.0.1:8790/...), never a tab in the person's
# regular browsing session — that substring only ever appears in THAT
# window's own command line, never a normal tab's, so matching on it
# is safe: it can't catch an unrelated Chrome window or tab.
_FACE_APP_MARKER = "--app=http://127.0.0.1:8790"


def _kill_by_port(port: int, label: str) -> bool:
    """Finds whatever's LISTENING on `port` and ends it. True if a
    process was found and asked to end; False if nothing was there or
    the kill failed. Never raises."""
    try:
        out = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError) as e:
        log(f"[launch] netstat failed while looking for {label}: {e}")
        return False
    pid = None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") \
                and "LISTENING" in line:
            pid = parts[-1]
            break
    if not pid:
        return False
    try:
        subprocess.run(["taskkill", "/PID", pid, "/T", "/F"],
                       capture_output=True, timeout=5)
        log(f"[launch] {label} (pid {pid}) closed")
        return True
    except (OSError, subprocess.SubprocessError) as e:
        log(f"[launch] failed to close {label} (pid {pid}): {e}")
        return False


def _kill_face_window() -> bool:
    """Closes the dedicated Chrome app window talk.bat opens for the
    face, found by its unique --app= command line (see
    _FACE_APP_MARKER) so a normal browsing window/tab is never at
    risk. Best-effort, never raises."""
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='chrome.exe'", "get",
             "ProcessId,CommandLine", "/format:list"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    if not out.strip():
        # wmic is deprecated/missing on newer Windows — CIM is the
        # modern replacement, via a one-off PowerShell call.
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter "
                 "\"Name='chrome.exe'\" | ForEach-Object "
                 "{ \"CommandLine=$($_.CommandLine)`nProcessId=$($_.ProcessId)\" }"],
                capture_output=True, text=True, timeout=8,
            ).stdout
        except (OSError, subprocess.SubprocessError) as e:
            log(f"[launch] couldn't enumerate chrome.exe: {e}")
            return False
    pid = None
    cur_cmdline = ""
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("CommandLine="):
            cur_cmdline = line[len("CommandLine="):]
        elif line.startswith("ProcessId=") and _FACE_APP_MARKER in cur_cmdline:
            pid = line[len("ProcessId="):].strip()
            break
    if not pid:
        return False
    try:
        subprocess.run(["taskkill", "/PID", pid, "/T", "/F"],
                       capture_output=True, timeout=5)
        log(f"[launch] face window (pid {pid}) closed")
        return True
    except (OSError, subprocess.SubprocessError) as e:
        log(f"[launch] failed to close the face window (pid {pid}): {e}")
        return False


def kill_face() -> bool:
    """Best-effort shutdown of everything talk.bat opened besides the
    voice line itself: the ai-visualizer server, the dedicated Chrome
    app window showing it, and jarvis-bridge. True if at least one of
    them was found and closed. Never raises."""
    closed_server = _kill_by_port(_FACE_PORT, "the face server")
    closed_window = _kill_face_window()
    closed_bridge = _kill_by_port(_BRIDGE_PORT, "the bridge")
    return closed_server or closed_window or closed_bridge

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
"""One-time setup: makes the cold-start "Hey Jarvis" listener
(wake_listener.py) start automatically at every Windows login.

Drops a shortcut into the current user's Startup folder pointing at
wake_listener.bat, minimized. No admin rights needed (this is the
per-user Startup folder, not the all-users one) and no Scheduled Task,
so it's a normal shortcut anyone can see, move, or delete themselves
in shell:startup.

Run:  uv run python install_autostart.py          (install)
      uv run python install_autostart.py --remove (undo)
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
SHORTCUT_NAME = "Jarvis wake-word listener.lnk"


def _startup_dir() -> Path:
    import ctypes
    CSIDL_STARTUP = 7
    buf = ctypes.create_unicode_buffer(260)
    ctypes.windll.shell32.SHGetFolderPathW(None, CSIDL_STARTUP, None, 0, buf)
    return Path(buf.value)


def install():
    target = _startup_dir() / SHORTCUT_NAME
    bat = REPO / "wake_listener.bat"
    if not bat.is_file():
        print(f"wake_listener.bat not found at {bat}")
        sys.exit(1)
    import win32com.client  # pywin32
    shell = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortcut(str(target))
    shortcut.TargetPath = str(bat)
    shortcut.WorkingDirectory = str(REPO)
    shortcut.WindowStyle = 7  # minimized
    shortcut.Description = "Listens for \"Hey Jarvis\" and starts the voice stack"
    shortcut.Save()
    print(f"Installed: {target}")
    print("The listener will start automatically at your next login.")
    print("To start it right now without logging out: double-click "
          f"{bat.name} in {REPO}.")


def remove():
    target = _startup_dir() / SHORTCUT_NAME
    if target.exists():
        target.unlink()
        print(f"Removed: {target}")
    else:
        print("Not installed — nothing to remove.")


if __name__ == "__main__":
    if sys.platform != "win32":
        print("Windows only.")
        sys.exit(1)
    if "--remove" in sys.argv:
        remove()
    else:
        install()

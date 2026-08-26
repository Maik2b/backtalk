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
"""Screen capture — "look at my screen", by voice or a hotkey.

Windows only, and deliberately dependency-free: shells out to
PowerShell's own System.Windows.Forms/System.Drawing (the same
subprocess-not-a-new-pip-package approach signals.py uses for audio
playback) instead of pulling in Pillow just for one screenshot call.

Captures the FULL virtual desktop (every monitor, one image), saves a
PNG under logs/screenshots/ for your own records, and returns it
base64-encoded so brain.py can attach it to the next turn as an image
content block.
"""
import base64
import datetime
import os
import subprocess

from backtalk.config import CFG, REPO
from backtalk.vlog import log

SHOT_DIR = os.path.join(REPO, "logs", "screenshots")

_PS_SCRIPT = r"""
Add-Type -AssemblyName System.Windows.Forms,System.Drawing
$screens = [System.Windows.Forms.Screen]::AllScreens
$bounds = [System.Drawing.Rectangle]::Empty
foreach ($s in $screens) { $bounds = [System.Drawing.Rectangle]::Union($bounds, $s.Bounds) }
$bmp = New-Object System.Drawing.Bitmap $bounds.Width, $bounds.Height
$gfx = [System.Drawing.Graphics]::FromImage($bmp)
$gfx.CopyFromScreen($bounds.Location, [System.Drawing.Point]::Empty, $bounds.Size)
$bmp.Save('__PATH__', [System.Drawing.Imaging.ImageFormat]::Png)
$gfx.Dispose()
$bmp.Dispose()
"""


def capture() -> tuple[str, str] | None:
    """Grabs the whole virtual desktop. Returns (base64_png, saved_path),
    or None on any failure (no PowerShell, no display, permission
    denied) — the caller decides how to tell the person it didn't work.
    Never raises."""
    os.makedirs(SHOT_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    path = os.path.join(SHOT_DIR, f"{stamp}.png")
    script = _PS_SCRIPT.replace("__PATH__", path.replace("'", "''"))
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0 or not os.path.isfile(path):
            log(f"[screen] capture failed: {result.stderr.strip()[:200]}")
            return None
    except (OSError, subprocess.SubprocessError) as e:
        log(f"[screen] capture failed: {e}")
        return None
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        log(f"[screen] could not read saved screenshot: {e}")
        return None
    return base64.b64encode(data).decode("ascii"), path


def prune_old_screenshots(keep=50):
    """Screenshots accumulate fast if this gets used a lot; keep only
    the most recent N. Called after each capture. Never raises."""
    try:
        files = sorted(
            (os.path.join(SHOT_DIR, f) for f in os.listdir(SHOT_DIR)
             if f.lower().endswith(".png")),
            key=os.path.getmtime,
        )
        for f in files[:-keep]:
            os.remove(f)
    except OSError:
        pass

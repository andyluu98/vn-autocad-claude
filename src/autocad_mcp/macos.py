"""macOS support for the File IPC backend (full AutoCAD for Mac).

AutoCAD for Mac runs AutoLISP but has no COM/ActiveX and no Win32 window
messages, so the dispatcher is triggered by typing the MCPD command into
AutoCAD through System Events (osascript). AutoCAD LT for Mac has no
AutoLISP and cannot be driven this way.

Requires macOS privacy permissions for the app that launches the MCP server
(Terminal, iTerm, Claude Desktop...):
- Accessibility: to type into AutoCAD.
- Screen Recording: only for screenshots.
"""

from __future__ import annotations

import base64
import io
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import structlog

log = structlog.get_logger()

# Vowel-free alias defined in mcp_dispatch.lsp. Vietnamese Telex/VNI input
# sources rewrite words that contain vowels ("dispatch" -> "díaptch"), so the
# typed trigger must not contain any.
MAC_TRIGGER_COMMAND = "MCPD"

# Matches the app bundle in a process path, e.g.
# /Applications/Autodesk/AutoCAD 2025/AutoCAD 2025.app/Contents/MacOS/AutoCAD
_BUNDLE_RE = re.compile(r"/(AutoCAD[^/]*)\.app/Contents/MacOS/")

OSASCRIPT_TIMEOUT = 10.0


def parse_autocad_apps(ps_output: str) -> tuple[list[str], list[str]]:
    """Split running AutoCAD bundles from `ps -axo comm=` into (full, lt)."""
    full: list[str] = []
    lt: list[str] = []
    for line in ps_output.splitlines():
        m = _BUNDLE_RE.search(line)
        if not m:
            continue
        name = m.group(1)
        bucket = lt if name.upper().startswith("AUTOCAD LT") else full
        if name not in bucket:
            bucket.append(name)
    return full, lt


def find_autocad_app() -> str | None:
    """Return the app name of a running full AutoCAD for Mac (e.g. 'AutoCAD 2025')."""
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(
            ["ps", "-axo", "comm="], capture_output=True, text=True, timeout=5
        ).stdout
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("mac_ps_failed", error=str(e))
        return None
    full, lt = parse_autocad_apps(out)
    if not full and lt:
        log.info("mac_autocad_lt_only", apps=lt, hint="AutoCAD LT for Mac has no AutoLISP")
    return full[0] if full else None


def _as_string(s: str) -> str:
    """Quote a Python string as an AppleScript string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_trigger_script(app_name: str, restore_focus: bool = True) -> str:
    """AppleScript that brings AutoCAD forward and types the dispatch trigger.

    Two ESC (key code 53) cancel any pending command, as on Windows.
    Return is key code 36. The previously frontmost app is reactivated
    afterwards so the user's terminal/chat keeps focus.
    """
    lines = [
        'tell application "System Events" to set prevApp to name of first application process whose frontmost is true',
        f"tell application {_as_string(app_name)} to activate",
        "delay 0.15",
        'tell application "System Events"',
        "    key code 53",
        "    key code 53",
        "    delay 0.05",
        f"    keystroke {_as_string(MAC_TRIGGER_COMMAND)}",
        "    key code 36",
        "end tell",
    ]
    if restore_focus:
        lines += [
            "delay 0.1",
            f"if prevApp is not {_as_string(app_name)} then",
            '    tell application "System Events" to set frontmost of application process prevApp to true',
            "end if",
        ]
    return "\n".join(lines)


def run_osascript(script: str) -> tuple[bool, str]:
    """Run an AppleScript; return (ok, stderr-or-stdout)."""
    try:
        r = subprocess.run(
            ["osascript", "-"], input=script, capture_output=True, text=True,
            timeout=OSASCRIPT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    if r.returncode != 0:
        return False, r.stderr.strip()
    return True, r.stdout.strip()


def explain_osascript_error(err: str) -> str:
    """Turn common osascript failures into an actionable hint."""
    low = err.lower()
    if "not allowed" in low or "-1743" in err or "-25211" in err or "assistive" in low:
        return (
            f"{err}\nGrant permission: System Settings > Privacy & Security > "
            "Accessibility (and Automation > System Events) for the app that runs "
            "the MCP server (Terminal, iTerm, Claude...), then restart it."
        )
    return err


def send_trigger(app_name: str, restore_focus: bool = True) -> bool:
    """Type the MCPD trigger into AutoCAD for Mac."""
    ok, msg = run_osascript(build_trigger_script(app_name, restore_focus))
    if not ok:
        log.error("mac_dispatch_trigger_failed", error=explain_osascript_error(msg))
    return ok


def build_bounds_script(app_name: str) -> str:
    """AppleScript returning 'x, y, w, h' of AutoCAD's front window after activating it."""
    return "\n".join([
        f"tell application {_as_string(app_name)} to activate",
        "delay 0.3",
        'tell application "System Events"',
        "    set p to first application process whose frontmost is true",
        "    set {x, y} to position of front window of p",
        "    set {w, h} to size of front window of p",
        "end tell",
        'return (x as text) & "," & (y as text) & "," & (w as text) & "," & (h as text)',
    ])


def parse_bounds(text: str) -> tuple[int, int, int, int] | None:
    """Parse 'x, y, w, h' (osascript output) into ints."""
    try:
        parts = [int(float(p)) for p in text.replace(" ", "").split(",")]
    except ValueError:
        return None
    if len(parts) != 4 or parts[2] <= 0 or parts[3] <= 0:
        return None
    return parts[0], parts[1], parts[2], parts[3]


class MacScreenshotProvider:
    """Capture the AutoCAD for Mac front window with `screencapture -R`."""

    def __init__(self, app_name: str):
        self._app = app_name

    def capture(self) -> str | None:
        if sys.platform != "darwin":
            return None
        ok, out = run_osascript(build_bounds_script(self._app))
        if not ok:
            log.warning("mac_window_bounds_failed", error=explain_osascript_error(out))
            return None
        bounds = parse_bounds(out)
        if not bounds:
            log.warning("mac_window_bounds_bad", output=out)
            return None
        x, y, w, h = bounds
        with tempfile.TemporaryDirectory() as tmp:
            png = Path(tmp) / "autocad.png"
            try:
                subprocess.run(
                    ["screencapture", "-x", "-t", "png", "-R", f"{x},{y},{w},{h}", str(png)],
                    capture_output=True, timeout=10,
                )
            except (OSError, subprocess.SubprocessError) as e:
                log.warning("mac_screencapture_failed", error=str(e))
                return None
            if not png.exists() or png.stat().st_size == 0:
                log.warning(
                    "mac_screencapture_empty",
                    hint="Grant Screen Recording permission to the app that runs the MCP server.",
                )
                return None
            try:
                from PIL import Image

                from autocad_mcp.screenshot import _downscale

                img = _downscale(Image.open(png).convert("RGB"))
                buf = io.BytesIO()
                img.save(buf, format="PNG", optimize=True)
                return base64.b64encode(buf.getvalue()).decode("ascii")
            except Exception as e:
                log.warning("mac_screenshot_encode_failed", error=str(e))
                return None

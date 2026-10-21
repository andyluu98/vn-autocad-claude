"""Backend detection and environment configuration."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import structlog

log = structlog.get_logger()

# Paths
LISP_DIR = Path(__file__).resolve().parent.parent.parent / "lisp-code"
# Must match *mcp-ipc-dir* in mcp_dispatch.lsp (C:/temp/ on Windows, /tmp/autocad_mcp/ on Mac)
_DEFAULT_IPC_DIR = "C:/temp" if sys.platform == "win32" else "/tmp/autocad_mcp"
IPC_DIR = Path(os.environ.get("AUTOCAD_MCP_IPC_DIR", _DEFAULT_IPC_DIR))

# Backend selection
BACKEND_DEFAULT = "auto"  # auto | file_ipc | ezdxf

# IPC timeout (seconds), clamped to [1, 300]
IPC_TIMEOUT = max(1.0, min(300.0, float(os.environ.get("AUTOCAD_MCP_IPC_TIMEOUT", "10.0"))))

# Screenshot
ONLY_TEXT_FEEDBACK = os.environ.get("AUTOCAD_MCP_ONLY_TEXT", "").lower() in ("1", "true", "yes")

# Max screenshot width in pixels — downscale bigger captures to save tokens/context.
# Set to 0 to disable downscaling. Clamped to [320, 4096].
try:
    _shot_w = int(os.environ.get("AUTOCAD_MCP_SCREENSHOT_MAX_WIDTH", "1400"))
except ValueError:
    _shot_w = 1400
SCREENSHOT_MAX_WIDTH = 0 if _shot_w <= 0 else max(320, min(4096, _shot_w))

# Platform availability
WIN32_AVAILABLE = sys.platform == "win32"
MACOS_AVAILABLE = sys.platform == "darwin"


def _current_backend_env() -> str:
    """Read backend selection from env with normalization."""
    return os.environ.get("AUTOCAD_MCP_BACKEND", BACKEND_DEFAULT).strip().lower()


def _is_wsl() -> bool:
    """Detect WSL Linux runtime."""
    if os.environ.get("WSL_INTEROP"):
        return True
    try:
        return "microsoft" in os.uname().release.lower()
    except AttributeError:
        return False


def _write_debug_snapshot(backend_env: str):
    """Optionally write backend detection debug information.

    Set AUTOCAD_MCP_DEBUG_DETECT_FILE to enable.
    """
    debug_file = os.environ.get("AUTOCAD_MCP_DEBUG_DETECT_FILE", "").strip()
    if not debug_file:
        return

    try:
        debug_path = Path(debug_file)
        debug_path.parent.mkdir(parents=True, exist_ok=True)
        with debug_path.open("w", encoding="utf-8") as f:
            f.write(f"sys.platform={sys.platform}\n")
            f.write(f"WIN32_AVAILABLE={WIN32_AVAILABLE}\n")
            f.write(f"BACKEND_ENV={backend_env}\n")
            f.write(f"python={sys.executable}\n")
    except Exception:
        # Best-effort only; never fail backend detection due debug writes.
        pass


def detect_backend() -> str:
    """Return the backend name to use: 'file_ipc' or 'ezdxf'.

    Raises RuntimeError with actionable message if explicit backend fails.
    """
    backend_env = _current_backend_env()
    _write_debug_snapshot(backend_env)

    if backend_env == "ezdxf":
        return "ezdxf"

    if backend_env in ("auto", "file_ipc"):
        if WIN32_AVAILABLE:
            try:
                from autocad_mcp.backends.file_ipc import find_autocad_window

                hwnd = find_autocad_window()
                if hwnd:
                    log.info("autocad_window_found", hwnd=hwnd)
                    return "file_ipc"
                elif backend_env == "file_ipc":
                    raise RuntimeError(
                        "AUTOCAD_MCP_BACKEND=file_ipc but no AutoCAD window found. "
                        "Start AutoCAD LT and open a .dwg file."
                    )
            except ImportError:
                if backend_env == "file_ipc":
                    raise RuntimeError(
                        "AUTOCAD_MCP_BACKEND=file_ipc requires pywin32. "
                        "Install with: pip install pywin32"
                    )
                log.info("win32_deps_missing_fallback_ezdxf")
        elif MACOS_AVAILABLE:
            from autocad_mcp.macos import find_autocad_app

            app = find_autocad_app()
            if app:
                log.info("autocad_mac_found", app=app)
                return "file_ipc"
            elif backend_env == "file_ipc":
                raise RuntimeError(
                    "AUTOCAD_MCP_BACKEND=file_ipc but no AutoCAD for Mac is running. "
                    "Start AutoCAD (full version; AutoCAD LT for Mac has no AutoLISP) "
                    "and open a .dwg file."
                )
        elif backend_env == "file_ipc":
            raise RuntimeError(
                "AUTOCAD_MCP_BACKEND=file_ipc requires Windows or macOS. "
                "Use AUTOCAD_MCP_BACKEND=ezdxf for headless mode."
            )
        elif _is_wsl():
            log.info(
                "wsl_linux_python_fallback_ezdxf",
                platform=sys.platform,
                python=sys.executable,
                hint="Launch MCP with Windows python.exe for File IPC backend.",
            )

    log.info("using_ezdxf_backend")
    return "ezdxf"

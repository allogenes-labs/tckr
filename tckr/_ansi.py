"""Tiny zero-dependency ANSI helper — color + Windows VT plumbing.

Just enough to make the `tckr status` dashboard look good without pulling in
`rich`/`colorama`. Every call site stays color-agnostic: pass `enabled` through
`colorize` / `paint` and it no-ops to plain text when color is off, so the same
render path serves both a TTY and a piped/`NO_COLOR` run.
"""
from __future__ import annotations

import os
import sys

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"

# 256-color foreground codes — a neon "cyberpunk" palette.
CYAN = "\x1b[38;5;51m"
MAGENTA = "\x1b[38;5;201m"
GREEN = "\x1b[38;5;46m"
RED = "\x1b[38;5;196m"
YELLOW = "\x1b[38;5;226m"
ORANGE = "\x1b[38;5;208m"
GREY = "\x1b[38;5;245m"
BLUE = "\x1b[38;5;39m"

# Cyan→magenta vertical gradient for the logo (one code per logo line).
GRADIENT = (
    "\x1b[38;5;51m",
    "\x1b[38;5;45m",
    "\x1b[38;5;39m",
    "\x1b[38;5;99m",
    "\x1b[38;5;171m",
    "\x1b[38;5;201m",
)


def supports_color(stream=None) -> bool:
    """True if we should emit ANSI to `stream` (default stdout).

    Honors the `NO_COLOR` (off) and `FORCE_COLOR` (on) conventions, otherwise
    only colorizes a real TTY so redirected/piped output stays clean.
    """
    if stream is None:
        stream = sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def enable_windows_vt() -> None:
    """Enable ANSI escape processing on legacy Windows consoles (cmd.exe).

    Modern Windows Terminal / PowerShell already handle VT sequences; this is a
    best-effort rescue for older hosts. Silently no-ops everywhere else or on any
    failure — the caller has already decided color is wanted.
    """
    if os.name != "nt":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        # STD_OUTPUT_HANDLE = -11; ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:  # pragma: no cover - platform/host dependent
        pass


def colorize(text: str, code: str, enabled: bool) -> str:
    """Wrap `text` in `code`…RESET, or return it unchanged when not `enabled`."""
    if not enabled or not code:
        return text
    return f"{code}{text}{RESET}"


# Short alias used throughout the renderer.
paint = colorize

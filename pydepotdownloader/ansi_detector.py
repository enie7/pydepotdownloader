# Ported from the C# AnsiDetector, which was copied from
# https://github.com/spectreconsole/spectre.console
# MIT License - Copyright(c) 2020 Patrik Svensson, Phil Scott, Nils Andresen

import ctypes
import os
import re
import sys

_REGEXES = [
    re.compile(r"^xterm"),  # xterm, PuTTY, Mintty
    re.compile(r"^rxvt"),  # RXVT
    re.compile(r"^eterm"),  # Eterm
    re.compile(r"^screen"),  # GNU screen, tmux
    re.compile(r"tmux"),  # tmux
    re.compile(r"^vt100"),  # DEC VT series
    re.compile(r"^vt102"),  # DEC VT series
    re.compile(r"^vt220"),  # DEC VT series
    re.compile(r"^vt320"),  # DEC VT series
    re.compile(r"ansi"),  # ANSI
    re.compile(r"scoansi"),  # SCO ANSI
    re.compile(r"cygwin"),  # Cygwin, MinGW
    re.compile(r"linux"),  # Linux console
    re.compile(r"konsole"),  # Konsole
    re.compile(r"bvterm"),  # Bitvise SSH Client
    re.compile(r"^st-256color"),  # Suckless Simple Terminal, st
    re.compile(r"alacritty"),  # Alacritty
]

_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
_DISABLE_NEWLINE_AUTO_RETURN = 0x0008


def detect(std_error=False, upgrade=True):
    """Returns a (supports_ansi, legacy_console) tuple."""
    if sys.platform == "win32":
        # Running under ConEmu?
        con_emu = os.environ.get("ConEmuANSI")
        if con_emu and con_emu.lower() == "on":
            return True, False

        return _windows_supports_ansi(upgrade, std_error)

    return _detect_from_term()


def _detect_from_term():
    # Check if the terminal is of type ANSI/VT100/xterm compatible.
    term = os.environ.get("TERM")
    if term and term.strip():
        if any(rgx.search(term) for rgx in _REGEXES):
            return True, False

    return False, True


def _windows_supports_ansi(upgrade, std_error):
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-12 if std_error else -11)

        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            # Could not get console mode, try TERM (set in cygwin, WSL-Shell).
            ansi_from_term, legacy_from_term = _detect_from_term()
            return ansi_from_term, legacy_from_term if ansi_from_term else False

        if not upgrade:
            return False, True

        # Try enable ANSI support.
        new_mode = mode.value | _ENABLE_VIRTUAL_TERMINAL_PROCESSING | _DISABLE_NEWLINE_AUTO_RETURN
        if not kernel32.SetConsoleMode(handle, new_mode):
            # Enabling failed.
            return False, True

        return True, False
    except Exception:
        # All we know here is that we don't support ANSI.
        return False, False

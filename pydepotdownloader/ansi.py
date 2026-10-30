# This file is subject to the terms and conditions defined
# in file 'LICENSE', which is part of this source code package.

import enum
import sys

from . import ansi_detector

ESC = "\x1b"
BEL = "\x07"


# https://conemu.github.io/en/AnsiEscapeCodes.html#ConEmu_specific_OSC
# https://learn.microsoft.com/en-us/windows/terminal/tutorials/progress-bar-sequences
class ProgressState(enum.IntEnum):
    HIDDEN = 0
    DEFAULT = 1
    ERROR = 2
    INDETERMINATE = 3
    WARNING = 4


_use_progress = False


def init():
    global _use_progress

    if not sys.stdin.isatty() or not sys.stdout.isatty():
        return

    if sys.platform.startswith("linux"):
        return

    supports_ansi, legacy_console = ansi_detector.detect(std_error=False, upgrade=True)

    _use_progress = supports_ansi and not legacy_console


def progress(downloaded, total):
    value = round(downloaded / float(total) * 100.0) if total else 0
    progress_state(ProgressState.DEFAULT, min(255, max(0, int(value))))


def progress_state(state, value=0):
    if not _use_progress:
        return

    sys.stdout.write(f"{ESC}]9;4;{int(state)};{value}{BEL}")
    sys.stdout.flush()

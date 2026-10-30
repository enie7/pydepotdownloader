# This file is subject to the terms and conditions defined
# in file 'LICENSE', which is part of this source code package.

import ctypes
import os
import stat
import sys

MODE_EXECUTE = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH


def set_executable(path, value):
    if sys.platform == "win32":
        return

    mode = stat.S_IMODE(os.stat(path).st_mode)
    has_execute_mask = (mode & MODE_EXECUTE) == MODE_EXECUTE
    if has_execute_mask != value:
        os.chmod(path, mode | MODE_EXECUTE if value else mode & ~MODE_EXECUTE)


def verify_console_launch():
    # Reference: https://devblogs.microsoft.com/oldnewthing/20160125-00/?p=92922
    if sys.platform != "win32":
        return

    try:
        kernel32 = ctypes.windll.kernel32
        process_list = (ctypes.c_uint32 * 2)()
        process_count = kernel32.GetConsoleProcessList(process_list, 2)

        if process_count != 1:
            return

        ctypes.windll.user32.MessageBoxW(
            None,
            "Depot Downloader is a console application; there is no GUI.\n\n"
            "If you do not pass any command line parameters, it prints usage info and exits.\n\n"
            "You must use this from a terminal/console.",
            "Depot Downloader",
            0x00000000 | 0x00000030,  # MB_OK | MB_ICONWARNING
        )
    except Exception:
        pass

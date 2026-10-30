# This file is subject to the terms and conditions defined
# in file 'LICENSE', which is part of this source code package.
"""Reads login credentials from a local file, so they don't have to be typed on the command line.

Two formats are accepted. Plain key/value lines:

    # comments and blank lines are ignored
    username = myaccount
    password = my password

or a JSON object:

    {"username": "myaccount", "password": "my password"}

Recognised keys are `username` (also `user`) and `password` (also `pass`). A file with no keys at
all is read positionally, username on the first line and password on the second, which is what
`.secret` style files look like.
"""

import json
import os
import stat
import sys

DEFAULT_FILE_NAMES = ["credentials.txt", "credentials.json"]

_KEY_ALIASES = {
    "username": "username",
    "user": "username",
    "password": "password",
    "pass": "password",
}


def find_default_file():
    """Returns the first of the default credentials files that exists in the working directory."""
    for file_name in DEFAULT_FILE_NAMES:
        if os.path.isfile(file_name):
            return file_name

    return None


def load(file_name):
    """Returns a (username, password) tuple, either of which may be None."""
    with open(file_name, encoding="utf-8-sig") as handle:
        contents = handle.read()

    warn_if_world_readable(file_name)

    values = _parse_json(contents) if contents.lstrip().startswith("{") else _parse_lines(contents)

    return values.get("username"), values.get("password")


def warn_if_world_readable(file_name):
    if sys.platform == "win32":
        return

    try:
        mode = stat.S_IMODE(os.stat(file_name).st_mode)
    except OSError:
        return

    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        print(
            f"Warning: '{file_name}' contains a password and is readable by other users, "
            f"consider running: chmod 600 {file_name}",
            file=sys.stderr,
        )


def _parse_json(contents):
    values = {}

    for key, value in json.loads(contents).items():
        name = _KEY_ALIASES.get(key.strip().lower())

        if name is not None and value is not None:
            values[name] = str(value)

    return values


def _parse_lines(contents):
    lines = []

    for line in contents.splitlines():
        line = line.strip()

        if not line or line.startswith("#") or line.startswith(";"):
            continue

        lines.append(line)

    values = {}

    for line in lines:
        separator = min((line.index(c) for c in "=:" if c in line), default=-1)

        if separator == -1:
            continue

        name = _KEY_ALIASES.get(line[:separator].strip().lower())

        if name is not None:
            # Only the surrounding whitespace is stripped, passwords may contain anything else
            values[name] = line[separator + 1 :].strip()

    if values:
        return values

    # No recognised keys, so this is a bare file with the username and password on their own lines.
    # Checking for keys first keeps passwords containing '=' or ':' from being mistaken for one.
    for name, line in zip(("username", "password"), lines):
        values[name] = line

    return values

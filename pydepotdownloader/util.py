# This file is subject to the terms and conditions defined
# in file 'LICENSE', which is part of this source code package.

import hashlib
import os
import platform
import sys
import zlib

from steam.core.crypto import symmetric_decrypt_ecb  # noqa: F401  (re-exported)
from steam.core.manifest import DepotManifest

from . import proto_manifest


def get_steam_os():
    if sys.platform == "win32":
        return "windows"

    if sys.platform == "darwin":
        return "macos"

    if sys.platform.startswith("linux"):
        return "linux"

    if sys.platform.startswith("freebsd"):
        # Return linux as freebsd steam client doesn't exist yet
        return "linux"

    return "unknown"


def get_steam_arch():
    return "64" if platform.machine().endswith("64") or sys.maxsize > 2**32 else "32"


def read_password():
    """Read a password from the console, echoing '*' for each character."""
    password = []

    if sys.platform == "win32":
        import msvcrt

        while True:
            char = msvcrt.getwch()

            if char in ("\r", "\n"):
                break
            if char == "\b":
                if password:
                    password.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            if char == "\x03":
                raise KeyboardInterrupt
            if " " <= char <= "~":
                password.append(char)
                sys.stdout.write("*")
                sys.stdout.flush()

        return "".join(password)

    import termios
    import tty

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)

    try:
        tty.setraw(fd)

        while True:
            char = sys.stdin.read(1)

            if char in ("\r", "\n"):
                break
            if char in ("\x7f", "\b"):
                if password:
                    password.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            if char == "\x03":
                raise KeyboardInterrupt
            if " " <= char <= "~":
                password.append(char)
                sys.stdout.write("*")
                sys.stdout.flush()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    return "".join(password)


def write_file_atomic(filename, data, mode=0o666):
    """Writes the whole file or nothing: a crash midway leaves the previous contents in place. mode is
    filtered through the umask, as with open()."""
    temp_filename = f"{filename}.tmp"

    # A leftover from an earlier crash would keep its own permissions
    if os.path.exists(temp_filename):
        os.remove(temp_filename)

    with open(os.open(temp_filename, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode), "wb") as fs:
        fs.write(data)
        fs.flush()
        os.fsync(fs.fileno())

    os.replace(temp_filename, filename)


def run_in_thread(pool, func, *args):
    """Runs func on a gevent thread pool and waits for it without blocking other greenlets. Failures come back
    as a value so that the thread pool does not log them itself, they are re-raised on the calling greenlet."""
    result, error = pool.apply(_capture_result, (func, args))

    if error is not None:
        raise error

    return result


def _capture_result(func, args):
    try:
        return func(*args), None
    except BaseException as ex:
        return None, ex


def validate_steam3_file_checksums(fs, chunkdata):
    """Validate a file against Steam3 chunk data, returning the chunks that need downloading."""
    needed_chunks = []

    for data in chunkdata:
        fs.seek(data.offset, os.SEEK_SET)

        if adler_hash(fs.read(data.cb_original)) != data.crc:
            needed_chunks.append(data)

    return needed_chunks


def adler_hash(data):
    """Adler32 as used by Steam, which seeds with zero rather than one."""
    return zlib.adler32(data, 0) & 0xFFFFFFFF


def file_sha_hash(filename):
    sha = hashlib.sha1()

    with open(filename, "rb") as fs:
        for block in iter(lambda: fs.read(1024 * 1024), b""):
            sha.update(block)

    return sha.digest()


def load_manifest_from_file(directory, depot_id, manifest_id, bad_hash_warning):
    # Try loading Steam format manifest first.
    filename = os.path.join(directory, f"{depot_id}_{manifest_id}.manifest")

    if os.path.exists(filename):
        try:
            with open(filename + ".sha", "rb") as fs:
                expected_checksum = fs.read()
        except OSError:
            expected_checksum = None

        current_checksum = file_sha_hash(filename)

        if expected_checksum is not None and expected_checksum == current_checksum:
            with open(filename, "rb") as fs:
                return DepotManifest(fs.read())
        elif bad_hash_warning:
            print(f"Manifest {manifest_id} on disk did not match the expected checksum.")

    # Try converting legacy manifest format.
    filename = os.path.join(directory, f"{depot_id}_{manifest_id}.bin")

    if os.path.exists(filename):
        try:
            with open(filename + ".sha", "rb") as fs:
                expected_checksum = fs.read()
        except OSError:
            expected_checksum = None

        old_manifest, current_checksum = proto_manifest.load_from_file(filename)

        if old_manifest is not None and (expected_checksum is None or expected_checksum != current_checksum):
            old_manifest = None

            if bad_hash_warning:
                print(f"Manifest {manifest_id} on disk did not match the expected checksum.")

        if old_manifest is not None:
            return old_manifest.convert_to_steam_manifest(depot_id)

    return None


def save_manifest_to_file(directory, manifest):
    try:
        filename = os.path.join(directory, f"{manifest.depot_id}_{manifest.gid}.manifest")

        with open(filename, "wb") as fs:
            fs.write(manifest.serialize(compress=False))

        with open(filename + ".sha", "wb") as fs:
            fs.write(file_sha_hash(filename))

        return True  # If serialization completes without throwing an exception, return true
    except Exception:
        return False  # Return false if an error occurs


def decode_hex_string(hex_string):
    if hex_string is None:
        return None

    return bytes.fromhex(hex_string)

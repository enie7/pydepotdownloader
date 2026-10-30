# This file is subject to the terms and conditions defined
# in file 'LICENSE', which is part of this source code package.

import os

from . import protobuf_net, util


class AccountSettingsStore:
    def __init__(self):
        self.content_server_penalty = {}
        self.login_tokens = _CaseInsensitiveDict()
        self.guard_data = _CaseInsensitiveDict()
        self.file_name = None

    @staticmethod
    def load_from_file(filename):
        instance = AccountSettingsStore()

        if os.path.exists(filename):
            try:
                with open(filename, "rb") as fs:
                    data = protobuf_net.deflate_decompress(fs.read())

                for field_number, value in protobuf_net.iter_fields(data):
                    if field_number == 2:
                        host, penalty = protobuf_net.read_map(value, True, False)
                        instance.content_server_penalty[host] = penalty
                    elif field_number == 4:
                        account, token = protobuf_net.read_map(value, True, True)
                        instance.login_tokens[account] = token
                    elif field_number == 5:
                        account, guard_data = protobuf_net.read_map(value, True, True)
                        instance.guard_data[account] = guard_data
            except (OSError, ValueError, IndexError) as ex:
                print(f"Failed to load account settings: {ex}")
                instance = AccountSettingsStore()

        instance.file_name = filename

        return instance

    def save(self):
        data = protobuf_net.write_map(2, self.content_server_penalty, True, False)
        data += protobuf_net.write_map(4, dict(self.login_tokens), True, True)
        data += protobuf_net.write_map(5, dict(self.guard_data), True, True)

        try:
            directory = os.path.dirname(self.file_name)

            if directory:
                os.makedirs(directory, exist_ok=True)

            # Holds login tokens, so only the owner gets to read it
            util.write_file_atomic(self.file_name, protobuf_net.deflate_compress(data), 0o600)
        except OSError as ex:
            print(f"Failed to save account settings: {ex}")


class _CaseInsensitiveDict(dict):
    """Mirrors the StringComparer.OrdinalIgnoreCase dictionaries used by the C# store."""

    def __init__(self, *args, **kwargs):
        super().__init__()
        self.update(dict(*args, **kwargs))

    def __setitem__(self, key, value):
        super().__setitem__(key.lower() if isinstance(key, str) else key, value)

    def __getitem__(self, key):
        return super().__getitem__(key.lower() if isinstance(key, str) else key)

    def __delitem__(self, key):
        super().__delitem__(key.lower() if isinstance(key, str) else key)

    def __contains__(self, key):
        return super().__contains__(key.lower() if isinstance(key, str) else key)

    def get(self, key, default=None):
        return super().get(key.lower() if isinstance(key, str) else key, default)

    def pop(self, key, *args):
        return super().pop(key.lower() if isinstance(key, str) else key, *args)

    def update(self, other):
        for key, value in other.items():
            self[key] = value

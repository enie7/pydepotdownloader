# This file is subject to the terms and conditions defined
# in file 'LICENSE', which is part of this source code package.

import os

from . import protobuf_net, util


class DepotConfigStore:
    def __init__(self):
        self.installed_manifest_ids = {}
        self.file_name = None

    @staticmethod
    def load_from_file(filename):
        instance = DepotConfigStore()

        if os.path.exists(filename):
            with open(filename, "rb") as fs:
                data = protobuf_net.deflate_decompress(fs.read())

            for field_number, value in protobuf_net.iter_fields(data):
                if field_number == 1:
                    depot_id, manifest_id = protobuf_net.read_map(value, False, False)
                    instance.installed_manifest_ids[depot_id] = manifest_id

        instance.file_name = filename

        return instance

    def save(self):
        data = protobuf_net.write_map(1, self.installed_manifest_ids, False, False)

        util.write_file_atomic(self.file_name, protobuf_net.deflate_compress(data))

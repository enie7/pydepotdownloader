# This file is subject to the terms and conditions defined
# in file 'LICENSE', which is part of this source code package.
"""Reader for the legacy DepotDownloader manifest format (`<depot>_<manifest>.bin`).

Those files are protobuf-net serialized and deflate compressed. This module decodes
them so that old installations can be updated without re-downloading everything.
"""

import hashlib
import os
import struct
import zlib

from steam.core.manifest import DepotManifest

# protobuf-net serializes System.DateTime as bcl.DateTime, where the value is
# relative to the unix epoch and scaled by the following units.
_SCALE_TO_SECONDS = {
    0: 24 * 60 * 60,  # days
    1: 60 * 60,  # hours
    2: 60,  # minutes
    3: 1,  # seconds
    4: 1e-3,  # milliseconds
    5: 1e-7,  # ticks
}


def _read_varint(data, pos):
    result = 0
    shift = 0

    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift

        if not byte & 0x80:
            return result, pos

        shift += 7


def _iter_fields(data):
    """Yield (field_number, wire_type, value) for a protobuf message."""
    pos = 0

    while pos < len(data):
        key, pos = _read_varint(data, pos)
        field_number, wire_type = key >> 3, key & 0x07

        if wire_type == 0:  # varint
            value, pos = _read_varint(data, pos)
        elif wire_type == 1:  # 64-bit
            value, pos = data[pos : pos + 8], pos + 8
        elif wire_type == 2:  # length delimited
            length, pos = _read_varint(data, pos)
            value, pos = data[pos : pos + length], pos + length
        elif wire_type == 5:  # 32-bit
            value, pos = data[pos : pos + 4], pos + 4
        else:
            raise ValueError(f"Unsupported protobuf wire type {wire_type}")

        yield field_number, wire_type, value


def _zigzag_decode(value):
    return (value >> 1) ^ -(value & 1)


def _parse_datetime(data):
    value = 0
    scale = 0

    for field_number, _, field_value in _iter_fields(data):
        if field_number == 1:
            value = _zigzag_decode(field_value)
        elif field_number == 2:
            scale = field_value

    return value * _SCALE_TO_SECONDS.get(scale, 1)


class ChunkData:
    def __init__(self):
        self.chunk_id = b""
        self.checksum = b"\x00\x00\x00\x00"
        self.offset = 0
        self.compressed_length = 0
        self.uncompressed_length = 0

    @classmethod
    def parse(cls, data):
        chunk = cls()

        for field_number, _, value in _iter_fields(data):
            if field_number == 1:
                chunk.chunk_id = value
            elif field_number == 2:
                chunk.checksum = value
            elif field_number == 3:
                chunk.offset = value
            elif field_number == 4:
                chunk.compressed_length = value
            elif field_number == 5:
                chunk.uncompressed_length = value

        return chunk


class FileData:
    def __init__(self):
        self.file_name = ""
        self.chunks = []
        self.flags = 0
        self.total_size = 0
        self.file_hash = b""

    @classmethod
    def parse(cls, data):
        file_data = cls()

        for field_number, _, value in _iter_fields(data):
            if field_number == 1:
                file_data.file_name = value.decode("utf-8")
            elif field_number == 2:
                file_data.chunks.append(ChunkData.parse(value))
            elif field_number == 3:
                file_data.flags = value
            elif field_number == 4:
                file_data.total_size = value
            elif field_number == 5:
                file_data.file_hash = value

        return file_data


class ProtoManifest:
    def __init__(self):
        self.files = []
        self.id = 0
        self.creation_time = 0

    @classmethod
    def parse(cls, data):
        manifest = cls()

        for field_number, _, value in _iter_fields(data):
            if field_number == 1:
                manifest.files.append(FileData.parse(value))
            elif field_number == 2:
                manifest.id = value
            elif field_number == 3:
                manifest.creation_time = _parse_datetime(value)

        return manifest

    def convert_to_steam_manifest(self, depot_id):
        uncompressed_size = compressed_size = 0
        new_manifest = DepotManifest()

        for file in self.files:
            new_file = new_manifest.payload.mappings.add()
            new_file.filename = file.file_name
            new_file.sha_filename = hashlib.sha1(file.file_name.replace("/", "\\").lower().encode("utf-8")).digest()
            new_file.flags = file.flags
            new_file.size = file.total_size
            new_file.sha_content = file.file_hash

            for chunk in file.chunks:
                new_chunk = new_file.chunks.add()
                new_chunk.sha = chunk.chunk_id
                new_chunk.crc = struct.unpack("<I", chunk.checksum)[0]
                new_chunk.offset = chunk.offset
                new_chunk.cb_compressed = chunk.compressed_length
                new_chunk.cb_original = chunk.uncompressed_length

                uncompressed_size += chunk.uncompressed_length
                compressed_size += chunk.compressed_length

        new_manifest.metadata.filenames_encrypted = False
        new_manifest.metadata.depot_id = depot_id
        new_manifest.metadata.gid_manifest = self.id
        new_manifest.metadata.creation_time = int(self.creation_time)
        new_manifest.metadata.cb_disk_original = uncompressed_size
        new_manifest.metadata.cb_disk_compressed = compressed_size
        new_manifest.metadata.crc_encrypted = 0

        return new_manifest


def load_from_file(filename):
    """Returns a (manifest, sha1 checksum) tuple, both None when the file is missing."""
    if not os.path.exists(filename):
        return None, None

    with open(filename, "rb") as fs:
        data = zlib.decompress(fs.read(), -zlib.MAX_WBITS)

    return ProtoManifest.parse(data), hashlib.sha1(data).digest()

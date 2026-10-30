# This file is subject to the terms and conditions defined
# in file 'LICENSE', which is part of this source code package.
"""Minimal protobuf-net compatible reader/writer for the config stores.

DepotDownloader persists its config files as protobuf-net serialized objects inside
a raw deflate stream. Dictionaries are written the same way protobuf writes maps:
a repeated message with the key in field 1 and the value in field 2.
"""

import zlib


def read_varint(data, pos):
    result = 0
    shift = 0

    while True:
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift

        if not byte & 0x80:
            return result, pos

        shift += 7


def write_varint(value):
    out = bytearray()

    while True:
        byte = value & 0x7F
        value >>= 7

        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def iter_fields(data):
    """Yield (field_number, value) pairs, where value is an int or bytes."""
    pos = 0

    while pos < len(data):
        key, pos = read_varint(data, pos)
        field_number, wire_type = key >> 3, key & 0x07

        if wire_type == 0:
            value, pos = read_varint(data, pos)
        elif wire_type == 1:
            value, pos = data[pos : pos + 8], pos + 8
        elif wire_type == 2:
            length, pos = read_varint(data, pos)
            value, pos = data[pos : pos + length], pos + length
        elif wire_type == 5:
            value, pos = data[pos : pos + 4], pos + 4
        else:
            raise ValueError(f"Unsupported protobuf wire type {wire_type}")

        yield field_number, value


def write_tag(field_number, wire_type):
    return write_varint((field_number << 3) | wire_type)


def write_length_delimited(field_number, value):
    return write_tag(field_number, 2) + write_varint(len(value)) + value


def write_map(field_number, mapping, key_is_string, value_is_string):
    out = bytearray()

    for key, value in mapping.items():
        entry = bytearray()

        if key_is_string:
            entry += write_length_delimited(1, key.encode("utf-8"))
        else:
            entry += write_tag(1, 0) + write_varint(key)

        if value_is_string:
            entry += write_length_delimited(2, value.encode("utf-8"))
        else:
            entry += write_tag(2, 0) + write_varint(value)

        out += write_length_delimited(field_number, bytes(entry))

    return bytes(out)


def read_map(data, key_is_string, value_is_string):
    key = "" if key_is_string else 0
    value = "" if value_is_string else 0

    for field_number, field_value in iter_fields(data):
        if field_number == 1:
            key = field_value.decode("utf-8") if key_is_string else field_value
        elif field_number == 2:
            value = field_value.decode("utf-8") if value_is_string else field_value

    return key, value


def deflate_compress(data):
    compressor = zlib.compressobj(9, zlib.DEFLATED, -zlib.MAX_WBITS)
    return compressor.compress(data) + compressor.flush()


def deflate_decompress(data):
    return zlib.decompress(data, -zlib.MAX_WBITS)

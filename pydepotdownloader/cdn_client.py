# This file is subject to the terms and conditions defined
# in file 'LICENSE', which is part of this source code package.
"""Port of SteamKit2.CDN.Client: downloading manifests and chunks from SteamPipe."""

import io
import os
import socket
import struct
import threading
import zipfile

import requests
from gevent.threadpool import ThreadPool
from steam.core.crypto import symmetric_decrypt
from steam.core.manifest import DepotManifest

try:
    # OpenSSL's AES is an order of magnitude faster than the pure pycryptodome build that
    # comes with the steam library, but it is optional, see _decrypt_chunk below
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:
    Cipher = None

try:
    import zstandard
except ImportError:
    zstandard = None

from . import http_client_factory, util

REQUEST_TIMEOUT = 10
RESPONSE_BODY_TIMEOUT = 60

MANIFEST_VERSION = 5

LANCACHE_TRIGGER_DOMAIN = "lancache.steamcontent.com"


class SteamKitWebRequestException(Exception):
    def __init__(self, message, status_code):
        super().__init__(message)
        self.status_code = status_code


class Client:
    def __init__(self, max_concurrency, use_lancache=False):
        concurrency = max(1, max_concurrency)
        self.use_lancache = use_lancache

        self.session = http_client_factory.create_http_client(pool_maxsize=max(concurrency, 10))

        # Decrypting and decompressing a chunk is pure CPU work, and gevent is cooperative: doing it
        # inline stalls every other download for its duration. AES, lzma, zstd and adler32 all drop
        # the GIL, so running them on real threads overlaps them with the transfers and with
        # each other.
        self._processing_pool = ThreadPool(min(os.cpu_count() or 4, concurrency))

    def download_manifest(
        self,
        depot_id,
        manifest_id,
        manifest_request_code,
        server,
        depot_key=None,
        proxy_server=None,
        cdn_auth_token=None,
    ):
        if manifest_request_code > 0:
            url = f"depot/{depot_id}/manifest/{manifest_id}/{MANIFEST_VERSION}/{manifest_request_code}"
        else:
            url = f"depot/{depot_id}/manifest/{manifest_id}/{MANIFEST_VERSION}"

        response = self._get(build_command(server, url, cdn_auth_token, proxy_server))

        # Decompress the zipped manifest data
        with zipfile.ZipFile(io.BytesIO(response)) as zip_file:
            depot_manifest = DepotManifest(zip_file.read(zip_file.filelist[0]))

        if depot_key is not None:
            # if we have the depot key, decrypt the manifest filenames
            depot_manifest.decrypt_filenames(depot_key)

        return depot_manifest

    def download_depot_chunk(self, depot_id, chunk, server, depot_key=None, proxy_server=None, cdn_auth_token=None):
        if not chunk.sha:
            raise ValueError("Chunk must have a ChunkID.")

        chunk_id = chunk.sha.hex()
        url = f"depot/{depot_id}/chunk/{chunk_id}"

        if self.use_lancache:
            data = self._get(
                build_lancache_command(url, cdn_auth_token),
                headers={"Host": server.host, "User-Agent": "Valve/Steam HTTP Client 1.0"},
            )
        else:
            data = self._get(build_command(server, url, cdn_auth_token, proxy_server))

        # assert that lengths match only if the chunk has a length assigned.
        if chunk.cb_compressed > 0 and len(data) != chunk.cb_compressed:
            raise OSError(
                f"Content-Length mismatch for depot chunk! (was {len(data)}, but should be {chunk.cb_compressed})"
            )

        if depot_key is None:
            return data

        return util.run_in_thread(self._processing_pool, process_chunk, chunk, data, depot_key)

    def _get(self, url, headers=None):
        try:
            response = self.session.get(
                url, headers=headers, timeout=(REQUEST_TIMEOUT, RESPONSE_BODY_TIMEOUT), stream=False
            )
        except requests.exceptions.Timeout as ex:
            raise TimeoutError(str(ex)) from ex

        if not response.ok:
            raise SteamKitWebRequestException(
                f"Response status code does not indicate success: {response.status_code} ({response.reason}).",
                response.status_code,
            )

        return response.content


def build_command(server, command, query, proxy_server):
    scheme = "http" if server.protocol == "http" else "https"
    host = server.vhost
    port = server.port
    path = command

    if proxy_server is not None and proxy_server.use_as_proxy and proxy_server.proxy_request_path_template:
        path_template = proxy_server.proxy_request_path_template
        path_template = path_template.replace("%host%", host)
        path_template = path_template.replace("%path%", f"/{path}")
        scheme = "http" if proxy_server.protocol == "http" else "https"
        host = proxy_server.vhost
        port = proxy_server.port
        path = path_template.lstrip("/")

    url = f"{scheme}://{host}:{port}/{path}"

    return f"{url}?{query}" if query else url


def build_lancache_command(command, query):
    url = f"http://{LANCACHE_TRIGGER_DOMAIN}:80/{command}"

    return f"{url}?{query}" if query else url


def detect_lancache_server():
    """Attempts to automatically resolve a Lancache on the local network.

    Will try to detect the Lancache through the poisoned DNS entry for lancache.steamcontent.com.
    """
    try:
        addresses = socket.getaddrinfo(LANCACHE_TRIGGER_DOMAIN, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False

    # If there are no private IPs, then there can't be a Lancache instance.
    # Lancache's IP must resolve to a private RFC 1918 address.
    return any(is_private_address(entry[4][0]) for entry in addresses)


def is_private_address(address):
    """Determines if an IP address is a private address, as specified in RFC1918."""
    import ipaddress

    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False

    if parsed.is_loopback or parsed.is_link_local:
        return True

    if parsed.version == 4:
        octets = parsed.packed

        if octets[0] == 10:
            return True
        if octets[0] == 172:
            return 16 <= octets[1] < 32
        if octets[0] == 192:
            return octets[1] == 168

        return False

    # Check for Unique Local Address (fc00::/7)
    return (parsed.packed[0] & 0xFE) == 0xFC


def process_chunk(chunk, data, depot_key):
    """Decrypts and decompresses a single depot chunk, port of SteamKit2.CDN.DepotChunk."""
    if len(depot_key) != 32:
        raise ValueError("Tried to decrypt depot chunk with non 32 byte key!")

    decrypted = _decrypt_chunk(data, depot_key)

    if len(decrypted) < 16:
        raise OSError(f"Not enough data in the decrypted depot chunk (was {len(decrypted)} bytes).")

    if decrypted[:4] == b"VSZa":  # Zstd
        decompressed = _decompress_vzstd(decrypted, chunk.cb_original)
    elif decrypted[:3] == b"VZa":  # LZMA
        decompressed = _decompress_vzip(decrypted, chunk.cb_original)
    elif decrypted[:4] == b"PK\x03\x04":  # PKzip
        with zipfile.ZipFile(io.BytesIO(decrypted)) as zip_file:
            decompressed = zip_file.read(zip_file.filelist[0])
    else:
        raise OSError(f"Unexpected depot chunk compression (first four bytes are {decrypted[:4].hex().upper()}).")

    if chunk.cb_original != len(decompressed):
        raise OSError(
            "Processed data checksum failed to decompressed to the expected chunk uncompressed length. "
            f"(was {len(decompressed)}, should be {chunk.cb_original})"
        )

    if util.adler_hash(decompressed) != chunk.crc:
        raise OSError(
            "Processed data checksum is incorrect! Downloaded depot chunk is corrupt or invalid/wrong depot key?"
        )

    return decompressed


_thread_state = threading.local()


def _decrypt_chunk(data, depot_key):
    """AES-256 with the IV encrypted in ECB ahead of the CBC payload, as Steam does it."""
    if Cipher is None:
        return symmetric_decrypt(data, depot_key)

    iv = Cipher(algorithms.AES(depot_key), modes.ECB()).decryptor().update(data[:16])
    decryptor = Cipher(algorithms.AES(depot_key), modes.CBC(iv)).decryptor()
    plaintext = decryptor.update(data[16:]) + decryptor.finalize()

    padding = plaintext[-1] if plaintext else 0

    if not 1 <= padding <= 16 or padding > len(plaintext):
        raise OSError("Invalid PKCS7 padding on decrypted chunk, wrong depot key?")

    return plaintext[:-padding]


def _decompress_vzstd(buffer, expected_size=None):
    if buffer[-3:] != b"zsv":
        raise OSError("Expecting VZstdFooter at end of stream")

    (size_decompressed,) = struct.unpack("<i", buffer[-11:-7])

    decompressed = _zstd_decompress(buffer[8:-15], expected_size or size_decompressed)

    if len(decompressed) != size_decompressed:
        raise OSError(f"Failed to decompress Zstd (expected {size_decompressed} bytes, got {len(decompressed)}).")

    return decompressed


def _zstd_decompress(data, expected_size):
    # Telling the decompressor how big the output is lets it allocate once instead of growing
    # its buffer, which is worth several times the throughput on chunk sized frames.
    if zstandard is not None:
        decompressor = getattr(_thread_state, "zstd", None)

        if decompressor is None:
            decompressor = _thread_state.zstd = zstandard.ZstdDecompressor()

        return decompressor.decompress(bytes(data), max_output_size=expected_size)

    try:
        from compression.zstd import decompress  # Python 3.14+
    except ImportError:
        raise OSError(
            "Zstd compressed chunk encountered, but no zstd support is available. "
            "Install the 'zstandard' package or use Python 3.14 or newer."
        ) from None

    return decompress(data)


def _decompress_vzip(buffer, expected_size=None):
    import lzma

    if buffer[2:3] != b"a":
        raise OSError("Expecting VZip version 'a'")

    if buffer[-2:] != b"zv":
        raise OSError("Expecting VZipFooter at end of stream")

    (size_decompressed,) = struct.unpack("<i", buffer[-6:-2])

    # 5 bytes of LZMA properties follow the header, and the footer is 10 bytes long
    filters = [lzma._decode_filter_properties(lzma.FILTER_LZMA1, buffer[7:12])]
    decompressor = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=filters)

    return decompressor.decompress(memoryview(buffer)[12:-10], max_length=expected_size or size_decompressed)

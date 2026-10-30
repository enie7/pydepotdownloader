from types import SimpleNamespace

import pytest

from pydepotdownloader import cdn_client, content_downloader, util
from pydepotdownloader.download_config import DownloadConfig
from pydepotdownloader.steam3_session import lowercase_keys


def server(**overrides):
    values = dict(
        protocol="https",
        vhost="cache1.steamcontent.com",
        port=443,
        use_as_proxy=False,
        proxy_request_path_template=None,
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def test_build_command():
    assert (
        cdn_client.build_command(server(), "depot/1/chunk/ab", "token=1", None)
        == "https://cache1.steamcontent.com:443/depot/1/chunk/ab?token=1"
    )


def test_build_command_through_proxy():
    proxy = server(
        protocol="http",
        vhost="proxy.local",
        port=80,
        use_as_proxy=True,
        proxy_request_path_template="/fetch/%host%%path%",
    )
    assert (
        cdn_client.build_command(server(), "depot/1/chunk/ab", None, proxy)
        == "http://proxy.local:80/fetch/cache1.steamcontent.com/depot/1/chunk/ab"
    )


@pytest.mark.parametrize(
    ("address", "private"),
    [
        ("10.0.0.1", True),
        ("172.16.5.4", True),
        ("172.32.0.1", False),
        ("192.168.1.1", True),
        ("8.8.8.8", False),
        ("127.0.0.1", True),
        ("not an ip", False),
    ],
)
def test_is_private_address(address, private):
    assert cdn_client.is_private_address(address) is private


def test_kv_get_is_case_insensitive():
    node = lowercase_keys({"Depots": {"Branches": {"Public": {"BuildID": "123"}}}})
    branches = content_downloader.kv_get(content_downloader.kv_get(node, "DEPOTS"), "branches")
    assert content_downloader.kv_get(content_downloader.kv_get(branches, "public"), "buildid") == "123"
    assert content_downloader.kv_get(node, "missing", "default") == "default"
    assert content_downloader.kv_get("not a dict", "x") is None


@pytest.mark.parametrize(
    ("value", "expected"), [("1", True), ("0", False), ("true", True), ("False", False), (None, False)]
)
def test_as_boolean(value, expected):
    assert content_downloader.as_boolean(value) is expected


def test_file_list_filter():
    import re

    config = DownloadConfig(
        using_file_list=True,
        files_to_download={"bin/game.exe"},
        files_to_download_regex=[re.compile(r"\.pak$", re.IGNORECASE)],
    )
    downloader = content_downloader.ContentDownloader(config, account_settings=None)

    assert downloader.test_is_file_included("BIN\\Game.exe")
    assert downloader.test_is_file_included("data/level1.PAK")
    assert not downloader.test_is_file_included("readme.txt")


def test_format_percent():
    assert content_downloader.format_percent(1, 4) == " 25.00%"
    assert content_downloader.format_percent(0, 0) == " 00.00%"


def test_adler_hash_uses_zero_seed():
    # Steam's Adler32 seeds with 0 rather than the standard 1
    assert util.adler_hash(b"") == 0
    assert util.adler_hash(b"abc") != __import__("zlib").adler32(b"abc")

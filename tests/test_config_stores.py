import os
import stat
import sys

import pytest

from pydepotdownloader import protobuf_net, util
from pydepotdownloader.account_settings_store import AccountSettingsStore
from pydepotdownloader.depot_config_store import DepotConfigStore


def test_varint_round_trip():
    for value in (0, 1, 127, 128, 300, 2**32 - 1, 0xFFFFFFFFFFFFFFFF):
        encoded = protobuf_net.write_varint(value)
        assert protobuf_net.read_varint(encoded, 0) == (value, len(encoded))


def test_deflate_round_trip():
    data = b"depot.config" * 100
    assert protobuf_net.deflate_decompress(protobuf_net.deflate_compress(data)) == data


def test_depot_config_store_round_trip(tmp_path):
    path = str(tmp_path / "depot.config")
    manifests = {731: 7617088375292372759, 1006: 0xFFFFFFFFFFFFFFFF}

    store = DepotConfigStore.load_from_file(path)
    store.installed_manifest_ids.update(manifests)
    store.save()

    assert DepotConfigStore.load_from_file(path).installed_manifest_ids == manifests
    assert not os.path.exists(path + ".tmp")


def test_account_settings_round_trip_is_private(tmp_path):
    path = str(tmp_path / "account.config")

    store = AccountSettingsStore.load_from_file(path)
    store.login_tokens["SomeUser"] = "token"
    store.guard_data["SomeUser"] = "guard"
    store.save()

    loaded = AccountSettingsStore.load_from_file(path)
    assert loaded.login_tokens["someuser"] == "token"
    assert loaded.guard_data.get("SOMEUSER") == "guard"

    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_atomic_write_keeps_old_contents_on_failure(tmp_path, monkeypatch):
    path = str(tmp_path / "depot.config")
    util.write_file_atomic(path, b"old")

    def fail(*args):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)

    with pytest.raises(OSError):
        util.write_file_atomic(path, b"new")

    with open(path, "rb") as fs:
        assert fs.read() == b"old"

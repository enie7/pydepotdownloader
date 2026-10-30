import os

import pytest

from pydepotdownloader import credentials_file


@pytest.fixture
def write(tmp_path):
    def _write(contents, name="credentials.txt"):
        path = tmp_path / name
        path.write_text(contents, encoding="utf-8")
        os.chmod(path, 0o600)
        return str(path)

    return _write


def test_key_value_lines(write):
    path = write("# comment\n\nusername = myaccount\npassword = my p@ss=word: x\n")
    assert credentials_file.load(path) == ("myaccount", "my p@ss=word: x")


def test_key_aliases_and_colon_separator(write):
    path = write("user: someone\n; another comment\npass: hunter2\n")
    assert credentials_file.load(path) == ("someone", "hunter2")


def test_json(write):
    path = write('{"username": "myaccount", "password": "my password"}', "credentials.json")
    assert credentials_file.load(path) == ("myaccount", "my password")


def test_positional_lines(write):
    path = write("myaccount\nweird=password\n")
    # A bare file must not have its password mistaken for a key/value line... unless the key is known
    assert credentials_file.load(path) == ("myaccount", "weird=password")


def test_missing_values(write):
    path = write("username = onlyuser\n")
    assert credentials_file.load(path) == ("onlyuser", None)


def test_world_readable_warning(write, capsys):
    path = write("username = a\npassword = b\n")
    os.chmod(path, 0o644)
    credentials_file.load(path)
    assert "chmod 600" in capsys.readouterr().err


def test_find_default_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert credentials_file.find_default_file() is None
    (tmp_path / "credentials.json").write_text("{}")
    assert credentials_file.find_default_file() == "credentials.json"

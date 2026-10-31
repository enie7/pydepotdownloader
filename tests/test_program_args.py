import pytest

from pydepotdownloader import program


def test_parameters_are_case_insensitive_and_consumed():
    reader = program.ArgumentReader(["-APP", "730", "-depot", "731", "732", "-validate"])

    assert reader.get_parameter("-app", converter=int) == 730
    assert reader.get_parameter_list("-depot", int) == [731, 732]
    assert reader.has_parameter("-validate")
    assert all(reader.consumed)


def test_missing_parameter_returns_default():
    reader = program.ArgumentReader(["-app"])
    assert reader.get_parameter("-app", 42, int) == 42
    assert reader.get_parameter("-branch") is None


def test_unconsumed_arguments_are_reported(capsys):
    reader = program.ArgumentReader(["-app", "730", "-bogus"])
    reader.get_parameter("-app", converter=int)
    reader.print_unconsumed_args()
    assert "Argument #3 -bogus was not used." in capsys.readouterr().err


def test_bad_numeric_argument_exits(capsys):
    reader = program.ArgumentReader(["-app", "potato"])
    with pytest.raises(SystemExit) as exc:
        reader.get_parameter("-app", converter=int)
    assert exc.value.code == 1
    assert "'potato' is not a valid value for -app" in capsys.readouterr().err


def test_no_arguments_prints_usage(capsys):
    assert program.main([]) == 0
    assert "Usage:" in capsys.readouterr().out


def test_version(capsys):
    assert program.main(["--version"]) == 0
    assert capsys.readouterr().out.startswith("pydepotdownloader v0.0.1 (port of DepotDownloader v3.4.0)")


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["-qr", "-username", "someone"], "-qr can not be used with -username"),
        (["-remember-password"], "-remember-password can not be used without -username or -qr"),
        (["-username", "x", "-password", "y"], "-app not specified"),
        (
            ["-app", "1", "-depot", "2", "3", "-manifest", "4", "-username", "x", "-password", "y"],
            "-manifest requires one id for every -depot",
        ),
    ],
)
def test_argument_validation(argv, message, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # keep account.config and credentials lookups out of the repo
    assert program.main(argv) == 1
    assert message in capsys.readouterr().out

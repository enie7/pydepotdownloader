# Contributing

Thanks for helping out! This project is a faithful port of
[SteamRE/DepotDownloader](https://github.com/SteamRE/DepotDownloader), so the guiding rule is:
**command line arguments, console output and on-disk formats should stay compatible with the C#
version** unless there is a documented reason to differ (see "Differences from the C# original" in
the README). When porting an upstream change, mention the upstream commit or PR in your PR.

## Development setup

```shell
python3 -m venv .venv && . .venv/bin/activate
pip install -e ".[speed,dev]"
```

## Checks

```shell
ruff check .          # lint
ruff format .         # format
pytest                # unit tests
```

CI runs all three on Linux, macOS and Windows across the supported Python versions.

The unit tests cover the pieces that don't need a Steam connection (argument parsing, credentials
files, config store formats, URL building, file filtering). Anything touching login or downloads
needs a manual test against real Steam content; please say in your PR what you tried, for example:

```shell
depotdownloader -app 1007 -depot 1006            # anonymous, Steamworks SDK redist
depotdownloader -app 1007 -depot 1006 -validate  # re-run, should download nothing
```

## Guidelines

* Never commit `credentials.txt`, `credentials.json` or `account.config`; they are git-ignored.
* Keep new modules mapped to their C# counterpart where one exists, and keep the comment style of
  the surrounding code.
* CPU-heavy work (decrypting, decompressing, hashing) must not run on the gevent hub; see
  `cdn_client.Client._processing_pool`.

## Reporting security issues

Please don't open a public issue for anything that could leak credentials or login tokens; contact
the maintainers privately instead.

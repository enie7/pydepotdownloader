# pydepotdownloader

A Python port of [SteamRE/DepotDownloader](https://github.com/SteamRE/DepotDownloader) 3.4.0.
Same command line arguments, same console output, same on-disk layout — it is a straight
translation of the C# program, with [ValvePython/steam](https://github.com/ValvePython/steam)
standing in for SteamKit2 as the Steam protocol library.

> **Disclaimer:** this is an unofficial tool, not affiliated with or endorsed by Valve. Only
> download content you are entitled to, and use it in line with the Steam Subscriber Agreement.

## Installation

Install from PyPI with pip, preferably into a virtual environment:

```shell
python3 -m venv .venv
. .venv/bin/activate                     # Windows: .venv\Scripts\activate

pip install "pydepotdownloader[speed]"   # installs the `depotdownloader` command
depotdownloader --version
```

Or from a checkout of the source:

```shell
git clone https://github.com/enie7/pydepotdownloader.git
cd pydepotdownloader
pip install ".[speed]"
```

Python 3.9+ is supported. The `speed` extra pulls in `cryptography` and `zstandard`, which are
optional but several times faster (see [Performance](#performance)); `pip install .` works without
them. On Python older than 3.14 `zstandard` is installed regardless, because some depots ship Zstd
compressed chunks and the standard library only gained a Zstd module in 3.14.

## Usage

```shell
depotdownloader -app <id> [-depot <id> [-manifest <id>]]
                [-username <username> [-password <password>]] [other options]
```

Without installing, `python3 -m pydepotdownloader` or `./depotdownloader.py` from the checkout
do the same thing (the dependencies still have to be installed, e.g. with
`pip install -r requirements.txt`).

### Example: a Project Zomboid dedicated server

Dedicated servers can usually be downloaded anonymously, without a Steam account. The Project
Zomboid server is app 380870:

```shell
# the Linux server into ./pzserver: depots 1006 (Steam runtime), 380871 (game files) and
# 380873 (Linux binaries), picked automatically from -os
depotdownloader -app 380870 -os linux -dir ./pzserver

# later: update it in place; only changed chunks are downloaded and removed files are deleted
depotdownloader -app 380870 -os linux -dir ./pzserver

# check every file against the manifest and repair anything damaged
depotdownloader -app 380870 -os linux -dir ./pzserver -validate

# an older branch of the game (branches: public, pre-release, legacy41, ...)
depotdownloader -app 380870 -os linux -dir ./pzserver-41 -branch legacy41

# the Windows server instead
depotdownloader -app 380870 -os windows -dir ./pzserver-windows
```

Without `-dir`, each depot goes to its own `depots/<depotid>/<buildid>/` folder instead of all of
them being merged into one directory, which is usually what you want for a server.

### Example: only some files, with `-filelist`

A file list names one file per line, as a path inside the depot. Exact paths are matched case
insensitively; lines starting with `regex:` are Python regular expressions, also case insensitive,
matched anywhere in the path (anchor them with `^` and `$`). Paths always use `/`, on Windows too.
There is no comment syntax, so keep the file to paths and patterns.

```text
ProjectZomboid64.json
regex:^media/lua/server/.*\.lua$
```

```shell
depotdownloader -app 380870 -os linux -dir ./pz-lua -filelist pz-files.txt
```

This downloads the server's launcher config and its ~300 server-side Lua scripts, about 4 MB
instead of several GB. Use `-manifest-only` first to get a text listing of every file in a depot
to pick from.

### More examples

```shell
# a single depot at a specific manifest
depotdownloader -app 730 -depot 731 -manifest 7617088375292372759 -username <user>

# a workshop item
depotdownloader -app 730 -pubfile 1885082371 -username <user>

# log in once and remember it, so later runs don't need the password
depotdownloader -app 1007 -username <user> -remember-password

# credentials read from ./credentials.txt, no username on the command line
depotdownloader -app 1007 -remember-password
```

### Using it from Python

The command line is a thin layer over `ContentDownloader`, which can be used directly. This is the
`-filelist` example above:

```python
import re

from pydepotdownloader.account_settings_store import AccountSettingsStore
from pydepotdownloader.content_downloader import ContentDownloader
from pydepotdownloader.download_config import DownloadConfig
from pydepotdownloader.errors import ContentDownloaderException

config = DownloadConfig(
    install_directory="pz-lua",
    using_file_list=True,
    files_to_download={"projectzomboid64.json"},  # exact paths, lower case
    files_to_download_regex=[re.compile(r"^media/lua/server/.*\.lua$", re.IGNORECASE)],
)
account_settings = AccountSettingsStore.load_from_file("account.config")
downloader = ContentDownloader(config, account_settings)

# No username or password: log in anonymously, which is enough for dedicated servers
if not downloader.initialize_steam3(None, None):
    raise SystemExit("Could not log in to Steam")

try:
    downloader.download_app(
        380870,  # Project Zomboid Dedicated Server
        depot_manifest_ids=[],  # empty: every depot that matches the OS below
        branch="public",
        os_override="linux",
        arch=None,
        language=None,
        lv=False,
        is_ugc=False,
    )
except ContentDownloaderException as ex:
    raise SystemExit(f"Download failed: {ex}") from ex
finally:
    downloader.shutdown_steam3()
```

`depot_manifest_ids` takes `(depot_id, manifest_id)` pairs to download specific depots, with
`content_downloader.INVALID_MANIFEST_ID` for "the branch's current manifest". Importing the package
applies gevent's socket monkey patching, so import it before anything else that opens sockets.
Progress is printed to stdout, as on the command line.

### All options

Run it with no arguments for the full parameter list; it is identical to the C# version:

Parameter                | Description
------------------------ | -----------
`-app <#>`               | the AppID to download.
`-depot <#>`             | the DepotID to download.
`-manifest <id>`         | manifest id of content to download (requires `-depot`, default: current for branch).
`-branch <branchname>`   | download from specified branch if available (default: `public`).
`-branchpassword <pass>` | branch password if applicable.
`-all-platforms`         | downloads all platform-specific depots when `-app` is used.
`-all-archs`             | download all architecture-specific depots when `-app` is used.
`-os <os>`               | the operating system for which to download the game.
`-osarch <arch>`         | the architecture for which to download the game (32 or 64).
`-all-languages`         | download all language-specific depots when `-app` is used.
`-language <lang>`       | the language for which to download the game (default: english).
`-lowviolence`           | download low violence depots when `-app` is used.
`-ugc <#>`               | the UGC ID to download.
`-pubfile <#>`           | the PublishedFileId to download.
`-username <user>`       | the username of the account to login to for restricted content.
`-password <pass>`       | the password of the account to login to for restricted content.
`-credentials <file>`    | read the username and password from a local file (default: `credentials.txt`).
`-remember-password`     | remember the login for subsequent runs of this user.
`-qr`                    | display a login QR code to be scanned with the Steam mobile app.
`-no-mobile`             | prefer entering a 2FA code instead of prompting to accept in the Steam mobile app.
`-dir <installdir>`      | the directory in which to place downloaded files.
`-filelist <file.txt>`   | list of files to download, one per line, `regex:` prefix for regex matching.
`-validate`              | include checksum verification of files already downloaded.
`-manifest-only`         | downloads a human readable manifest for any depots that would be downloaded.
`-cellid <#>`            | the overridden CellID of the content server to download from.
`-max-downloads <#>`     | maximum number of chunks to download concurrently (default: 48).
`-loginid <#>`           | a unique 32-bit integer Steam LogonID, required when running several instances.
`-use-lancache`          | forces downloads over the local network via a Lancache instance.
`-debug`                 | enable verbose debug logging.
`-V`, `--version`        | print version and runtime.

## Files on disk

Identical to the C# version:

* `depots/<depotid>/<buildid>/` — downloaded content (unless `-dir` is given)
* `<installdir>/.DepotDownloader/<depot>_<manifest>.manifest` — cached manifests, in Steam's own
  manifest format, so they are interchangeable with the C# DepotDownloader's cache
* `depots/.DepotDownloader/depot.config` (or `<installdir>/.DepotDownloader/depot.config`) —
  installed manifest ids, written in the same protobuf-net + deflate format as the C# version
* `account.config` — login tokens, Steam Guard data and content server penalties, written to the
  current directory with owner-only permissions (the C# version hides this in .NET isolated
  storage; the file format is the same)

`depot.config` and `account.config` are replaced atomically, so an interrupted run never leaves a
half-written file behind.

## Credentials file

Instead of passing `-username`/`-password`, put them in a local file. `credentials.txt` (or
`credentials.json`) in the current directory is picked up automatically; `-credentials <file>`
points at any other path. Anything given on the command line takes precedence over the file, and
`-qr` skips the file entirely.

```ini
# credentials.txt
username = myaccount
password = my password
```

or, equivalently:

```json
{"username": "myaccount", "password": "my password"}
```

`user`/`pass` work as key names too, `#` and `;` start a comment, and everything after the first
`=` or `:` is the password, so it may contain spaces and punctuation. A file with no key names at
all is read positionally — username on the first line, password on the second — so an existing
`.secret` style file works as `-credentials .secret`.

This is a plaintext password on disk: `chmod 600 credentials.txt` (a warning is printed if other
users can read it), and note that `credentials.txt`, `credentials.json` and `account.config` are in
`.gitignore` so they don't get committed. With `-remember-password` you only need the password once
— after the first login the refresh token in `account.config` is enough and the credentials file
can be deleted.

## Differences from the C# original

* **`account.config` and the credentials file live in the current directory**, next to `depots/`,
  rather than in .NET isolated storage. `-credentials` is the only added command line option.
* **Authentication** runs against the `IAuthenticationService` Web API endpoints instead of going
  over the CM connection, because the Python Steam library predates Steam's current auth flow.
  It is the same protocol and the same refresh tokens; `-qr`, `-remember-password`, Steam Guard
  codes and mobile confirmations all work the way they do in the C# version.
* **Password protected branches** are resolved through the older `encrypted_gid_2` scheme.
  SteamKit's newer `PICSGetPrivateBeta` request is not available in the Python Steam library, so
  branches whose depot section is fully private cannot be resolved and produce an explanatory error.
* **Downloads are spread over several CDN servers** (the best four by Steam's load figures)
  instead of one at a time, and a failing server is swapped out for the next one.
* **Depots overlap.** Chunks of all depots go through one download queue, and files are checked on
  disk while earlier chunks are still downloading, so the per-depot summary lines are printed at
  the end instead of after each depot.
* **`-use-lancache`** doesn't touch the concurrency limit. DepotDownloader raises it to 25 for a
  Lancache, which is below this port's default anyway.
* **Bad numeric arguments** (e.g. `-app potato`) print an error and exit 1 rather than throwing an
  unhandled .NET exception.
* **`-debug`** turns on Python logging for the Steam library instead of SteamKit's `DebugLog`.
* Concurrency uses gevent greenlets rather than .NET tasks; `-max-downloads` means the same thing,
  but defaults to 48 instead of 8 (see Performance below).

## Performance

What decides how fast a download goes, measured:

* **Concurrency.** A chunk request is mostly waiting on the server, so throughput scales with how
  many are in flight. Downloading a 2 GB depot (6.9 GB on disk) took 98 s at
  `-max-downloads 16` and 47 s at 48, which is the default; 96 was only slightly faster again.
  The C# version uses 8.
* **Chunk processing runs on worker threads**, not on the gevent hub, so decrypting and unpacking
  overlaps the transfers instead of stalling them. Worth about 2.5x.
* **Checking existing files runs on threads too**, and overlaps the downloads: repairing 40 damaged
  files in that depot (all 1008 files re-validated) went from 22.8 s to 18.2 s.
* **The AES and zstd backends matter enormously.** With `cryptography` and `zstandard` installed
  (the `speed` extra) chunk processing is roughly 4.5x faster than with pycryptodome and a
  default-constructed stdlib decompressor: AES went from 32 to 402 MiB/s and Zstd from 63 to
  382 MiB/s per core in testing, mostly by using OpenSSL and by telling zstd the output size up
  front so it allocates once. They are optional; without them the port still works, just slower.

What still caps it depends on the depot. A mostly incompressible 1.9 GB depot downloaded at about
110 MB/s, while the 2 GB one above, which unpacks to 3.5 times its size, levelled off around
40 MB/s of compressed data. Depots compressed with LZMA are also far more expensive to unpack
than newer Zstd ones, about 12 MiB/s of output per core versus 382, and no library choice fixes
that.

## Architecture

```
program.py                 CLI: parses DepotDownloader-style arguments into a DownloadConfig
  └─ content_downloader.py  ContentDownloader: depot selection, manifests, file diffing, chunk queue
       ├─ steam3_session.py  CM connection (ValvePython/steam): logon, PICS app/package info,
       │    │                depot keys, manifest request codes, CDN auth tokens, workshop lookups
       │    ├─ steam_auth.py              IAuthenticationService Web API (password, QR, Steam Guard)
       │    └─ console_authenticator.py   prompts for 2FA / email codes
       ├─ cdn_client_pool.py  CDN server list; spreads requests over the best servers, failover
       │    └─ cdn_client.py  HTTP fetch of manifests/chunks; AES + LZMA/Zstd on a thread pool
       ├─ depot_config_store.py / account_settings_store.py   on-disk state (protobuf-net + deflate)
       └─ util.py, proto_manifest.py   manifest cache, checksums, legacy .bin manifests
```

A download goes through four stages:

1. **Login.** `Steam3Session` connects to a Steam CM, authenticates (anonymously, with a saved
   refresh token from `account.config`, or through `steam_auth`) and collects the account's
   licenses.
2. **Resolve.** `ContentDownloader.download_app` fetches PICS app info, filters depots by
   OS/arch/language/low-violence, checks licenses and resolves each depot's manifest id for the
   branch, plus its depot key.
3. **Plan.** For every depot the manifest is loaded from the `.DepotDownloader` cache or
   downloaded from the CDN. Each file is compared against the previously installed manifest (or
   validated by Adler32 checksums) on a small thread pool to decide which chunks are needed;
   unchanged chunks are copied in place.
4. **Fetch.** Needed chunks are queued as soon as their file is planned, and `-max-downloads`
   greenlets work through the queue for all depots. Each chunk is downloaded from the least busy
   of the active CDN servers, decrypted and decompressed on a thread pool, and written at its
   offset. When everything is in, files that vanished since the previous manifest are deleted and
   `depot.config` records each depot's installed manifest id.

Concurrency is gevent (`steam.monkey.patch_minimal()` runs in `__init__.py`, before any socket is
created); only CPU or disk bound work leaves the hub. There is no global state: the CLI builds a
`DownloadConfig` and an `AccountSettingsStore` and hands them to a `ContentDownloader`, so the
package can also be driven from Python.

## Development

```shell
pip install -e ".[speed,dev]"
ruff check . && ruff format --check . && pytest
```

See [CONTRIBUTING.md](https://github.com/enie7/pydepotdownloader/blob/main/CONTRIBUTING.md) for details.

## Source layout

Each C# file has a matching Python module:

C# | Python
--- | ---
`Program.cs` | `pydepotdownloader/program.py`
`ContentDownloader.cs` | `pydepotdownloader/content_downloader.py`
`Steam3Session.cs` | `pydepotdownloader/steam3_session.py`
`CDNClientPool.cs` | `pydepotdownloader/cdn_client_pool.py`
SteamKit2 `CDN.Client` | `pydepotdownloader/cdn_client.py`
SteamKit2 `Authentication` | `pydepotdownloader/steam_auth.py`
`ConsoleAuthenticator.cs` | `pydepotdownloader/console_authenticator.py`
`AccountSettingsStore.cs` | `pydepotdownloader/account_settings_store.py`
`DepotConfigStore.cs` | `pydepotdownloader/depot_config_store.py`
`DownloadConfig.cs` | `pydepotdownloader/download_config.py`
`ProtoManifest.cs` | `pydepotdownloader/proto_manifest.py`
`Util.cs` | `pydepotdownloader/util.py`
`Ansi.cs`, `AnsiDetector.cs` | `pydepotdownloader/ansi.py`, `pydepotdownloader/ansi_detector.py`
`PlatformUtilities.cs` | `pydepotdownloader/platform_utilities.py`
`HttpClientFactory.cs` | `pydepotdownloader/http_client_factory.py`
(protobuf-net) | `pydepotdownloader/protobuf_net.py`
(new) | `pydepotdownloader/credentials_file.py`
(new) | `pydepotdownloader/errors.py`

## License

GPL-2.0, same as DepotDownloader, from which this is derived. See [LICENSE](https://github.com/enie7/pydepotdownloader/blob/main/LICENSE).

# This file is subject to the terms and conditions defined
# in file 'LICENSE', which is part of this source code package.

import logging
import platform
import re
import sys

from . import (
    UPSTREAM_VERSION,
    __version__,
    ansi,
    cdn_client,
    content_downloader,
    credentials_file,
    platform_utilities,
    util,
)
from .account_settings_store import AccountSettingsStore
from .content_downloader import ContentDownloader, ContentDownloaderException, OperationCancelledException
from .download_config import DEFAULT_MAX_DOWNLOADS, DownloadConfig


def main(args=None):
    if args is None:
        args = sys.argv[1:]

    if len(args) == 0:
        print_version()
        print_usage()

        if sys.platform == "win32":
            platform_utilities.verify_console_launch()

        return 0

    ansi.init()

    account_settings = AccountSettingsStore.load_from_file("account.config")

    # region Common Options

    # Not using has_parameter because it is case insensitive
    if len(args) == 1 and args[0] in ("-V", "--version"):
        print_version(True)
        return 0

    config = DownloadConfig()
    reader = ArgumentReader(args)
    has_parameter = reader.has_parameter
    get_parameter = reader.get_parameter
    get_parameter_list = reader.get_parameter_list

    if has_parameter("-debug"):
        print_version(True)

        logging.basicConfig(format="[%(name)s] %(message)s", level=logging.DEBUG)

    username = get_parameter("-username") or get_parameter("-user")
    password = get_parameter("-password") or get_parameter("-pass")

    credentials_path = get_parameter("-credentials")

    if credentials_path is None and not has_parameter("-qr"):
        credentials_path = credentials_file.find_default_file()

    if credentials_path is not None:
        try:
            file_username, file_password = credentials_file.load(credentials_path)

            # Anything given on the command line wins over the file
            username = username or file_username
            password = password or file_password

            print(f"Using credentials file: '{credentials_path}'.")
        except Exception as ex:
            print(f"Warning: Unable to load credentials file: {ex}")

    config.remember_password = has_parameter("-remember-password")
    config.use_qr_code = has_parameter("-qr")
    config.skip_app_confirmation = has_parameter("-no-mobile")

    if username is None:
        if config.remember_password and not config.use_qr_code:
            print("Error: -remember-password can not be used without -username or -qr.")
            return 1
    elif config.use_qr_code:
        print("Error: -qr can not be used with -username.")
        return 1

    config.download_manifest_only = has_parameter("-manifest-only")

    cell_id = get_parameter("-cellid", -1, int)
    if cell_id == -1:
        cell_id = 0

    config.cell_id = cell_id

    file_list = get_parameter("-filelist")

    if file_list is not None:
        REGEX_PREFIX = "regex:"

        try:
            config.using_file_list = True
            config.files_to_download = set()
            config.files_to_download_regex = []

            with open(file_list, encoding="utf-8-sig") as handle:
                files = handle.read().splitlines()

            for file_entry in files:
                if not file_entry.strip():
                    continue

                if file_entry.startswith(REGEX_PREFIX):
                    rgx = re.compile(file_entry[len(REGEX_PREFIX) :], re.IGNORECASE)
                    config.files_to_download_regex.append(rgx)
                else:
                    config.files_to_download.add(file_entry.replace("\\", "/").lower())

            print(f"Using filelist: '{file_list}'.")
        except Exception as ex:
            print(f"Warning: Unable to load filelist: {ex}")

    config.install_directory = get_parameter("-dir")

    config.verify_all = has_parameter("-verify-all") or has_parameter("-verify_all") or has_parameter("-validate")

    config.max_downloads = get_parameter("-max-downloads", DEFAULT_MAX_DOWNLOADS, int)

    if has_parameter("-use-lancache"):
        config.use_lancache = cdn_client.detect_lancache_server()

        if config.use_lancache:
            print("Detected Lancache server! Downloads will be directed through the Lancache.")

            # The C# version raises the concurrent downloads to 25 here, as Steam does. The default is already
            # higher than that.

    config.login_id = get_parameter("-loginid", None, int) if has_parameter("-loginid") else None

    # endregion

    app_id = get_parameter("-app", content_downloader.INVALID_APP_ID, int)
    if app_id == content_downloader.INVALID_APP_ID:
        print("Error: -app not specified!")
        return 1

    pub_file = get_parameter("-pubfile", content_downloader.INVALID_MANIFEST_ID, int)
    ugc_id = get_parameter("-ugc", content_downloader.INVALID_MANIFEST_ID, int)

    downloader = ContentDownloader(config, account_settings)

    if pub_file != content_downloader.INVALID_MANIFEST_ID:
        reader.print_unconsumed_args()

        return run_download(downloader, username, password, lambda: downloader.download_pubfile(app_id, pub_file))
    elif ugc_id != content_downloader.INVALID_MANIFEST_ID:
        reader.print_unconsumed_args()

        return run_download(downloader, username, password, lambda: downloader.download_ugc(app_id, ugc_id))
    else:
        # region App downloading

        branch = get_parameter("-branch") or get_parameter("-beta") or content_downloader.DEFAULT_BRANCH
        config.beta_password = get_parameter("-branchpassword") or get_parameter("-betapassword")

        if config.beta_password and not branch:
            print("Error: Cannot specify -branchpassword when -branch is not specified.")
            return 1

        config.download_all_platforms = has_parameter("-all-platforms")

        os_override = get_parameter("-os")

        if config.download_all_platforms and os_override:
            print("Error: Cannot specify -os when -all-platforms is specified.")
            return 1

        config.download_all_archs = has_parameter("-all-archs")

        arch = get_parameter("-osarch")

        if config.download_all_archs and arch:
            print("Error: Cannot specify -osarch when -all-archs is specified.")
            return 1

        config.download_all_languages = has_parameter("-all-languages")
        language = get_parameter("-language")

        if config.download_all_languages and language:
            print("Error: Cannot specify -language when -all-languages is specified.")
            return 1

        lv = has_parameter("-lowviolence")

        depot_manifest_ids = []
        is_ugc = False

        depot_id_list = get_parameter_list("-depot", int)
        manifest_id_list = get_parameter_list("-manifest", int)

        if manifest_id_list:
            if len(depot_id_list) != len(manifest_id_list):
                print("Error: -manifest requires one id for every -depot specified")
                return 1

            depot_manifest_ids.extend(zip(depot_id_list, manifest_id_list))
        else:
            depot_manifest_ids.extend((depot_id, content_downloader.INVALID_MANIFEST_ID) for depot_id in depot_id_list)

        reader.print_unconsumed_args()

        return run_download(
            downloader,
            username,
            password,
            lambda: downloader.download_app(
                app_id, depot_manifest_ids, branch, os_override, arch, language, lv, is_ugc
            ),
        )


def run_download(downloader, username, password, download):
    if not initialize_steam(downloader, username, password):
        print("Error: InitializeSteam failed")
        return 1

    try:
        download()
    except (ContentDownloaderException, OperationCancelledException) as ex:
        print(ex)
        return 1
    except Exception as ex:
        print(f"Download failed to due to an unhandled exception: {ex}")
        raise
    finally:
        downloader.shutdown_steam3()

    return 0


def initialize_steam(downloader, username, password):
    config = downloader.config

    if not config.use_qr_code:
        if (
            username is not None
            and password is None
            and (not config.remember_password or username not in downloader.account_settings.login_tokens)
        ):
            if username in downloader.account_settings.login_tokens:
                print(f'Account "{username}" has stored credentials. Did you forget to specify -remember-password?')

            while True:
                sys.stdout.write(f'Enter account password for "{username}": ')
                sys.stdout.flush()

                if not sys.stdin.isatty():
                    line = sys.stdin.readline()
                    # An empty read means end of input, matching Console.ReadLine() returning null
                    password = line.rstrip("\n") if line else None
                else:
                    # Avoid console echoing of password
                    password = util.read_password()

                print()

                if password != "":
                    break
        elif username is None:
            print("No username given. Using anonymous account with dedicated server subscription.")

    if password:
        MAX_PASSWORD_SIZE = 64

        if len(password) > MAX_PASSWORD_SIZE:
            print(
                f"Warning: Password is longer than {MAX_PASSWORD_SIZE} characters, which is not supported by Steam.",
                file=sys.stderr,
            )

        if not password.isascii():
            print("Warning: Password contains non-ASCII characters, which is not supported by Steam.", file=sys.stderr)

    return downloader.initialize_steam3(username, password)


class ArgumentReader:
    """DepotDownloader's own argument handling: case insensitive flags, remembering which arguments were used
    so that the unused ones can be reported."""

    def __init__(self, args):
        self.args = args
        self.consumed = [False] * len(self.args)

    def index_of_param(self, param):
        for index, arg in enumerate(self.args):
            if arg.lower() == param.lower():
                self.consumed[index] = True
                return index

        return -1

    def has_parameter(self, param):
        return self.index_of_param(param) > -1

    def get_parameter(self, param, default_value=None, converter=None):
        index = self.index_of_param(param)

        if index == -1 or index == len(self.args) - 1:
            return default_value

        str_param = self.args[index + 1]
        self.consumed[index + 1] = True

        if converter is None:
            return str_param

        return convert(str_param, converter, param)

    def get_parameter_list(self, param, converter=None):
        values = []
        index = self.index_of_param(param)

        if index == -1 or index == len(self.args) - 1:
            return values

        index += 1

        while index < len(self.args):
            str_param = self.args[index]

            if str_param.startswith("-"):
                break

            self.consumed[index] = True
            values.append(convert(str_param, converter, param) if converter is not None else str_param)

            index += 1

        return values

    def print_unconsumed_args(self):
        print_error = False

        for index, consumed in enumerate(self.consumed):
            if not consumed:
                print_error = True
                print(f"Argument #{index + 1} {self.args[index]} was not used.", file=sys.stderr)

        if print_error:
            print(
                "Make sure you specified the arguments correctly. Check --help for correct arguments.", file=sys.stderr
            )
            print(file=sys.stderr)


def convert(value, converter, param):
    try:
        return converter(value)
    except (TypeError, ValueError):
        print(f"Error: '{value}' is not a valid value for {param}.", file=sys.stderr)
        sys.exit(1)


def print_usage():
    # Do not use tabs to align parameters here because tab size may differ
    print()
    print("Usage: downloading one or all depots for an app:")
    print("       depotdownloader -app <id> [-depot <id> [-manifest <id>]]")
    print("                       [-username <username> [-password <password>]] [other options]")
    print()
    print("Usage: downloading a workshop item using pubfile id")
    print("       depotdownloader -app <id> -pubfile <id> [-username <username> [-password <password>]]")
    print("Usage: downloading a workshop item using ugc id")
    print("       depotdownloader -app <id> -ugc <id> [-username <username> [-password <password>]]")
    print()
    print("Parameters:")
    print("  -app <#>                 - the AppID to download.")
    print("  -depot <#>               - the DepotID to download.")
    print(
        "  -manifest <id>           - manifest id of content to download (requires -depot, default: current for branch)."
    )
    print(
        f"  -branch <branchname>    - download from specified branch if available (default: {content_downloader.DEFAULT_BRANCH})."
    )
    print("  -branchpassword <pass>   - branch password if applicable.")
    print("  -all-platforms           - downloads all platform-specific depots when -app is used.")
    print("  -all-archs               - download all architecture-specific depots when -app is used.")
    print(
        "  -os <os>                 - the operating system for which to download the game (windows, macos or linux, default: OS the program is currently running on)"
    )
    print(
        "  -osarch <arch>           - the architecture for which to download the game (32 or 64, default: the host's architecture)"
    )
    print("  -all-languages           - download all language-specific depots when -app is used.")
    print("  -language <lang>         - the language for which to download the game (default: english)")
    print("  -lowviolence             - download low violence depots when -app is used.")
    print()
    print("  -ugc <#>                 - the UGC ID to download.")
    print("  -pubfile <#>             - the PublishedFileId to download. (Will automatically resolve to UGC id)")
    print()
    print("  -username <user>         - the username of the account to login to for restricted content.")
    print("  -password <pass>         - the password of the account to login to for restricted content.")
    print(
        f"  -credentials <file>      - read the username and password from a local file (default: {credentials_file.DEFAULT_FILE_NAMES[0]} in the"
    )
    print("                             current directory, if it exists). lines look like `username = myaccount`.")
    print("  -remember-password       - if set, remember the password for subsequent logins of this user.")
    print("                             use -username <username> -remember-password as login credentials.")
    print("  -qr                      - display a login QR code to be scanned with the Steam mobile app")
    print(
        "  -no-mobile               - prefer entering a 2FA code instead of prompting to accept in the Steam mobile app"
    )
    print()
    print("  -dir <installdir>        - the directory in which to place downloaded files.")
    print(
        "  -filelist <file.txt>     - the name of a local file that contains a list of files to download (from the manifest)."
    )
    print(
        "                             prefix file path with `regex:` if you want to match with regex. each file path should be on their own line."
    )
    print()
    print("  -validate                - include checksum verification of files already downloaded")
    print("  -manifest-only           - downloads a human readable manifest for any depots that would be downloaded.")
    print("  -cellid <#>              - the overridden CellID of the content server to download from.")
    print(
        f"  -max-downloads <#>       - maximum number of chunks to download concurrently. (default: {DEFAULT_MAX_DOWNLOADS})."
    )
    print(
        "  -loginid <#>             - a unique 32-bit integer Steam LogonID in decimal, required if running multiple instances of DepotDownloader concurrently."
    )
    print("  -use-lancache            - forces downloads over the local network via a Lancache instance.")
    print()
    print("  -debug                   - enable verbose debug logging.")
    print("  -V or --version          - print version and runtime.")


def print_version(print_extra=False):
    print(f"pydepotdownloader v{__version__} (port of DepotDownloader v{UPSTREAM_VERSION})")

    if not print_extra:
        return

    print(f"Runtime: {platform.python_implementation()} {platform.python_version()} on {platform.platform()}")

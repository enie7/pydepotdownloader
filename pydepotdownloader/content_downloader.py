# This file is subject to the terms and conditions defined
# in file 'LICENSE', which is part of this source code package.

import logging
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import gevent
from gevent.lock import RLock, Semaphore
from gevent.pool import Pool
from gevent.queue import Queue
from gevent.threadpool import ThreadPool
from steam.enums import EDepotFileFlag

from . import ansi, cdn_client, http_client_factory, platform_utilities, util
from .cdn_client_pool import CDNClientPool
from .depot_config_store import DepotConfigStore
from .errors import ContentDownloaderException, OperationCancelledException
from .steam3_session import LogOnDetails, Steam3Session

INVALID_APP_ID = 0xFFFFFFFF
INVALID_DEPOT_ID = 0xFFFFFFFF
INVALID_MANIFEST_ID = 0xFFFFFFFFFFFFFFFF
DEFAULT_BRANCH = "public"

DEFAULT_DOWNLOAD_DIR = "depots"
CONFIG_DIR = ".DepotDownloader"
STAGING_DIR = os.path.join(CONFIG_DIR, "staging")

# Validating and allocating files is disk bound, a few threads are enough to keep ahead of the downloads
FILE_PLANNING_THREADS = min(4, os.cpu_count() or 1)

ALT_DIR_CHAR = "/" if os.sep == "\\" else "\\"

# EWorkshopFileType values that hold downloadable content
WORKSHOP_FILE_TYPE_COMMUNITY = 0
WORKSHOP_FILE_TYPE_COLLECTION = 2
WORKSHOP_FILE_TYPE_ART = 3
WORKSHOP_FILE_TYPE_SCREENSHOT = 5
WORKSHOP_FILE_TYPE_INTEGRATED_GUIDE = 10
WORKSHOP_FILE_TYPE_MERCH = 11
WORKSHOP_FILE_TYPE_CONTROLLER_BINDING = 12

SUPPORTED_WORKSHOP_FILE_TYPES = frozenset(
    [
        WORKSHOP_FILE_TYPE_COMMUNITY,
        WORKSHOP_FILE_TYPE_ART,
        WORKSHOP_FILE_TYPE_SCREENSHOT,
        WORKSHOP_FILE_TYPE_MERCH,
        WORKSHOP_FILE_TYPE_INTEGRATED_GUIDE,
        WORKSHOP_FILE_TYPE_CONTROLLER_BINDING,
    ]
)

_LOG = logging.getLogger("ContentDownloader")


class CancellationTokenSource:
    def __init__(self):
        self.is_cancellation_requested = False

    def cancel(self):
        self.is_cancellation_requested = True

    def throw_if_cancellation_requested(self):
        if self.is_cancellation_requested:
            raise OperationCancelledException("The operation was canceled.")


@dataclass
class DepotDownloadInfo:
    depot_id: int
    app_id: int
    manifest_id: int
    branch: str
    install_dir: str
    depot_key: bytes


class ManifestFile:
    """Wraps a manifest file mapping the way SteamKit's DepotManifest.FileData does."""

    __slots__ = ("mapping", "file_name")

    def __init__(self, mapping):
        self.mapping = mapping
        self.file_name = mapping.filename.rstrip("\x00 \n\t").replace(ALT_DIR_CHAR, os.sep)

    @property
    def total_size(self):
        return self.mapping.size

    @property
    def flags(self):
        return self.mapping.flags

    @property
    def file_hash(self):
        return self.mapping.sha_content

    @property
    def chunks(self):
        return self.mapping.chunks


@dataclass
class DepotDownloadCounter:
    complete_download_size: int = 0
    size_downloaded: int = 0
    depot_bytes_compressed: int = 0
    depot_bytes_uncompressed: int = 0
    lock: RLock = field(default_factory=RLock)


@dataclass
class DepotFilesData:
    depot_download_info: DepotDownloadInfo
    depot_counter: DepotDownloadCounter
    staging_dir: str
    manifest: object
    previous_manifest: object
    filtered_files: list
    all_file_names: set
    previous_files: list = None
    previous_files_by_name: dict = None


@dataclass
class FileStreamData:
    chunks_to_download: int
    file_stream: object = None
    file_lock: Semaphore = field(default_factory=lambda: Semaphore(1))


@dataclass
class GlobalDownloadCounter:
    complete_download_size: int = 0
    total_bytes_compressed: int = 0
    total_bytes_uncompressed: int = 0
    lock: RLock = field(default_factory=RLock)


def manifest_files(manifest):
    return [ManifestFile(mapping) for mapping in manifest.payload.mappings]


def kv_get(node, key, default=None):
    """Case insensitive lookup, mirroring SteamKit's KeyValue indexer. Relies on Steam3Session having lower cased
    the keys of all app and package info."""
    if not isinstance(node, dict):
        return default

    return node.get(key.lower(), default)


def as_boolean(value, default=False):
    if value is None:
        return default

    try:
        return int(value) != 0
    except (TypeError, ValueError):
        pass

    if isinstance(value, str):
        return value.strip().lower() == "true"

    return default


def as_unsigned_integer(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def format_percent(downloaded, total):
    percent = (downloaded / float(total)) * 100.0 if total else 0.0

    return f"{format(percent, '05.2f'):>6}%"


def format_creation_time(manifest):
    return datetime.fromtimestamp(manifest.metadata.creation_time, tz=timezone.utc).strftime("%m/%d/%Y %H:%M:%S")


class ContentDownloader:
    """Downloads apps, depots and workshop items. Port of the static ContentDownloader class, holding what
    the C# version keeps in static fields."""

    def __init__(self, config, account_settings):
        self.config = config
        self.account_settings = account_settings
        self.steam3 = None
        self.cdn_pool = None
        self.depot_config = None

    def create_directories(self, depot_id, depot_version):
        """Returns a (success, install_dir) tuple."""
        install_dir = None

        try:
            if not self.config.install_directory or not self.config.install_directory.strip():
                os.makedirs(DEFAULT_DOWNLOAD_DIR, exist_ok=True)

                depot_path = os.path.join(DEFAULT_DOWNLOAD_DIR, str(depot_id))
                os.makedirs(depot_path, exist_ok=True)

                install_dir = os.path.join(depot_path, str(depot_version))
                os.makedirs(install_dir, exist_ok=True)

                os.makedirs(os.path.join(install_dir, CONFIG_DIR), exist_ok=True)
                os.makedirs(os.path.join(install_dir, STAGING_DIR), exist_ok=True)
            else:
                os.makedirs(self.config.install_directory, exist_ok=True)

                install_dir = self.config.install_directory

                os.makedirs(os.path.join(install_dir, CONFIG_DIR), exist_ok=True)
                os.makedirs(os.path.join(install_dir, STAGING_DIR), exist_ok=True)
        except OSError:
            return False, None

        return True, install_dir

    def test_is_file_included(self, filename):
        if not self.config.using_file_list:
            return True

        filename = filename.replace("\\", "/")

        if filename.lower() in self.config.files_to_download:
            return True

        for rgx in self.config.files_to_download_regex:
            if rgx.search(filename):
                return True

        return False

    def account_has_access(self, app_id, depot_id):
        if (
            self.steam3 is None
            or self.steam3.steam_id is None
            or (self.steam3.licenses is None and not self.steam3.is_anonymous_account())
        ):
            return False

        if self.steam3.is_anonymous_account():
            license_query = [17906]
        else:
            license_query = list(dict.fromkeys(license.package_id for license in self.steam3.licenses))

        self.steam3.request_package_info(license_query)

        for license in license_query:
            package = self.steam3.package_info.get(license)

            if package:
                if any(as_unsigned_integer(child) == depot_id for child in kv_get(package, "appids", {}).values()):
                    return True

                if any(as_unsigned_integer(child) == depot_id for child in kv_get(package, "depotids", {}).values()):
                    return True

        # Check if this app is free to download without a license
        info = self.get_steam3_app_section(app_id, "common")
        if info is not None and as_boolean(kv_get(info, "FreeToDownload")):
            return True

        return False

    def get_steam3_app_section(self, app_id, section):
        if self.steam3 is None or self.steam3.app_info is None:
            return None

        app = self.steam3.app_info.get(app_id)

        if app is None:
            return None

        return kv_get(app, section)

    def get_steam3_app_build_number(self, app_id, branch):
        if app_id == INVALID_APP_ID:
            return 0

        depots = self.get_steam3_app_section(app_id, "depots")
        branches = kv_get(depots, "branches")
        node = kv_get(branches, branch)

        if node is None:
            return 0

        buildid = kv_get(node, "buildid")

        if buildid is None:
            return 0

        return as_unsigned_integer(buildid)

    def get_steam3_depot_proxy_app_id(self, depot_id, app_id):
        depots = self.get_steam3_app_section(app_id, "depots")
        depot_child = kv_get(depots, str(depot_id))

        if depot_child is None:
            return INVALID_APP_ID

        if kv_get(depot_child, "depotfromapp") is None:
            return INVALID_APP_ID

        return as_unsigned_integer(kv_get(depot_child, "depotfromapp"), INVALID_APP_ID)

    def get_steam3_depot_manifest(self, depot_id, app_id, branch):
        depots = self.get_steam3_app_section(app_id, "depots")
        depot_child = kv_get(depots, str(depot_id))

        if depot_child is None:
            return INVALID_MANIFEST_ID

        # Shared depots can either provide manifests, or leave you relying on their parent app.
        # It seems that with the latter, "sharedinstall" will exist (and equals 2 in the one existance I know of).
        # Rather than relay on the unknown sharedinstall key, just look for manifests. Test cases: 111710, 346680.
        if kv_get(depot_child, "manifests") is None and kv_get(depot_child, "depotfromapp") is not None:
            other_app_id = as_unsigned_integer(kv_get(depot_child, "depotfromapp"))

            if other_app_id == app_id:
                # This shouldn't ever happen, but ya never know with Valve. Don't infinite loop.
                print(f"App {app_id}, Depot {depot_id} has depotfromapp of {other_app_id}!")
                return INVALID_MANIFEST_ID

            self.steam3.request_app_info(other_app_id)

            return self.get_steam3_depot_manifest(depot_id, other_app_id, branch)

        manifests = kv_get(depot_child, "manifests")

        if not manifests:
            return INVALID_MANIFEST_ID

        node = kv_get(kv_get(manifests, branch, {}), "gid")

        # Non passworded branch, found the manifest
        if node is not None:
            return int(node)

        # If we requested public branch and it had no manifest, nothing to do
        if branch.lower() == DEFAULT_BRANCH:
            return INVALID_MANIFEST_ID

        # Either the branch just doesn't exist, or it has a password
        if not self.config.beta_password:
            print(f"Branch {branch} for depot {depot_id} was not found, either it does not exist or it has a password.")
            return INVALID_MANIFEST_ID

        if self.steam3.get_app_beta_password(branch) is None:
            # Submit the password to Steam now to get encryption keys
            self.steam3.check_app_beta_password(app_id, self.config.beta_password)

            if self.steam3.get_app_beta_password(branch) is None:
                print(f"Error: Password was invalid for branch {branch} (or the branch does not exist)")
                return INVALID_MANIFEST_ID

        # Got the password, the manifest id of a password protected branch is encrypted with it
        encrypted_gid = kv_get(kv_get(manifests, branch, {}), "encrypted_gid_2")

        if encrypted_gid is None:
            print(
                f"Error: Depot {depot_id} does not publish an encrypted manifest id for branch {branch}. "
                "Private beta depot sections are not supported."
            )
            return INVALID_MANIFEST_ID

        decrypted = util.symmetric_decrypt_ecb(bytes.fromhex(encrypted_gid), self.steam3.get_app_beta_password(branch))

        return int.from_bytes(decrypted[:8], "little")

    def get_app_name(self, app_id):
        info = self.get_steam3_app_section(app_id, "common")

        if info is None:
            return ""

        return kv_get(info, "name", "")

    def initialize_steam3(self, username, password):
        login_token = None

        if username is not None and self.config.remember_password:
            login_token = self.account_settings.login_tokens.get(username)

        self.steam3 = Steam3Session(
            LogOnDetails(
                username=username,
                password=password if login_token is None else None,
                should_remember_password=self.config.remember_password,
                access_token=login_token,
                login_id=self.config.login_id if self.config.login_id is not None else 0x534B32,  # "SK2"
            ),
            self.config,
            self.account_settings,
        )

        if not self.steam3.wait_for_credentials():
            print("Unable to get steam3 credentials.")
            return False

        return True

    def shutdown_steam3(self):
        if self.steam3 is None:
            return

        self.steam3.disconnect()

    def process_published_file(self, app_id, published_file_id, file_urls, content_file_ids):
        details = self.steam3.get_published_file_details(app_id, published_file_id)
        file_type = details.file_type if details is not None else None

        if file_type == WORKSHOP_FILE_TYPE_COLLECTION:
            for child in details.children:
                self.process_published_file(app_id, child.publishedfileid, file_urls, content_file_ids)
        elif file_type in SUPPORTED_WORKSHOP_FILE_TYPES:
            if details.file_url:
                file_urls.append((details.filename, details.file_url))
            elif details.hcontent_file > 0:
                content_file_ids.append(details.hcontent_file)
            else:
                print(f"Unable to locate manifest ID for published file {published_file_id}")
        else:
            print(f"Published file {published_file_id} has unsupported file type {file_type}. Skipping file")

    def download_pubfile(self, app_id, published_file_id):
        file_urls = []
        content_file_ids = []

        self.process_published_file(app_id, published_file_id, file_urls, content_file_ids)

        for file_name, url in file_urls:
            self.download_web_file(app_id, file_name, url)

        if content_file_ids:
            depot_manifest_ids = [(app_id, content_file_id) for content_file_id in content_file_ids]
            self.download_app(app_id, depot_manifest_ids, DEFAULT_BRANCH, None, None, None, False, True)

    def download_ugc(self, app_id, ugc_id):
        details = None

        if not self.steam3.is_anonymous_account():
            details = self.steam3.get_ugc_details(ugc_id)
        else:
            print(f"Unable to query UGC details for {ugc_id} from an anonymous account")

        if details is not None and details.url:
            self.download_web_file(app_id, details.filename, details.url)
        else:
            self.download_app(app_id, [(app_id, ugc_id)], DEFAULT_BRANCH, None, None, None, False, True)

    def download_web_file(self, app_id, file_name, url):
        created, install_dir = self.create_directories(app_id, 0)

        if not created:
            print("Error: Unable to create install directories!")
            return

        staging_dir = os.path.join(install_dir, STAGING_DIR)
        file_staging_path = os.path.join(staging_dir, file_name)
        file_final_path = os.path.join(install_dir, file_name)

        os.makedirs(os.path.dirname(file_final_path), exist_ok=True)
        os.makedirs(os.path.dirname(file_staging_path), exist_ok=True)

        client = http_client_factory.create_http_client()

        print(f"Downloading {file_name}")

        with open(file_staging_path, "wb") as file:
            with client.get(url, stream=True, timeout=(10, 60)) as response:
                response.raise_for_status()

                for block in response.iter_content(chunk_size=1024 * 1024):
                    file.write(block)

        if os.path.exists(file_final_path):
            os.remove(file_final_path)

        shutil.move(file_staging_path, file_final_path)

    def download_app(self, app_id, depot_manifest_ids, branch, os_override, arch, language, lv, is_ugc):
        self.cdn_pool = CDNClientPool(self.steam3, app_id, self.config, self.account_settings)

        # Load our configuration data containing the depots currently installed
        config_path = self.config.install_directory

        if not config_path or not config_path.strip():
            config_path = DEFAULT_DOWNLOAD_DIR

        os.makedirs(os.path.join(config_path, CONFIG_DIR), exist_ok=True)
        self.depot_config = DepotConfigStore.load_from_file(os.path.join(config_path, CONFIG_DIR, "depot.config"))

        if self.steam3 is not None:
            self.steam3.request_app_info(app_id)

        if not self.account_has_access(app_id, app_id):
            if not self.steam3.is_anonymous_account() and self.steam3.request_free_app_license(app_id):
                print(f"Obtained FreeOnDemand license for app {app_id}")

                # Fetch app info again in case we didn't get it fully without a license.
                self.steam3.request_app_info(app_id, True)
            else:
                content_name = self.get_app_name(app_id)
                raise ContentDownloaderException(f"App {app_id} ({content_name}) is not available from this account.")

        has_specific_depots = len(depot_manifest_ids) > 0
        depot_ids_found = []
        depot_ids_expected = [depot_id for depot_id, _ in depot_manifest_ids]
        depots = self.get_steam3_app_section(app_id, "depots")

        if is_ugc:
            workshop_depot = as_unsigned_integer(kv_get(depots, "workshopdepot"))

            if workshop_depot != 0 and workshop_depot not in depot_ids_expected:
                depot_ids_expected.append(workshop_depot)
                depot_manifest_ids = [(workshop_depot, manifest_id) for _, manifest_id in depot_manifest_ids]

            depot_ids_found.extend(depot_ids_expected)
        else:
            print(f"Using app branch: '{branch}'.")

            if depots is not None:
                for depot_section_name, depot_section in depots.items():
                    if not isinstance(depot_section, dict) or not depot_section:
                        continue

                    if not depot_section_name.isdigit():
                        continue

                    depot_id = int(depot_section_name)

                    if has_specific_depots and depot_id not in depot_ids_expected:
                        continue

                    if not has_specific_depots:
                        depot_config = kv_get(depot_section, "config")

                        if depot_config is not None:
                            if (
                                not self.config.download_all_platforms
                                and kv_get(depot_config, "oslist") is not None
                                and kv_get(depot_config, "oslist", "").strip()
                            ):
                                oslist = kv_get(depot_config, "oslist").split(",")
                                if (os_override or util.get_steam_os()) not in oslist:
                                    continue

                            if (
                                not self.config.download_all_archs
                                and kv_get(depot_config, "osarch") is not None
                                and kv_get(depot_config, "osarch", "").strip()
                            ):
                                depot_arch = kv_get(depot_config, "osarch")
                                if depot_arch != (arch or util.get_steam_arch()):
                                    continue

                            if (
                                not self.config.download_all_languages
                                and kv_get(depot_config, "language") is not None
                                and kv_get(depot_config, "language", "").strip()
                            ):
                                depot_lang = kv_get(depot_config, "language")
                                if depot_lang != (language or "english"):
                                    continue

                            if not lv and as_boolean(kv_get(depot_config, "lowviolence")):
                                continue

                    depot_ids_found.append(depot_id)

                    if not has_specific_depots:
                        depot_manifest_ids.append((depot_id, INVALID_MANIFEST_ID))

            if not depot_manifest_ids and not has_specific_depots:
                raise ContentDownloaderException(f"Couldn't find any depots to download for app {app_id}")

            if len(depot_ids_found) < len(depot_ids_expected):
                remaining_depot_ids = [depot_id for depot_id in depot_ids_expected if depot_id not in depot_ids_found]
                raise ContentDownloaderException(
                    f"Depot {', '.join(map(str, remaining_depot_ids))} not listed for app {app_id}"
                )

        infos = []

        for depot_id, manifest_id in depot_manifest_ids:
            info = self.get_depot_info(depot_id, app_id, manifest_id, branch)

            if info is not None:
                infos.append(info)

        print()

        try:
            self.download_steam3(infos)
        except OperationCancelledException:
            print(f"App {app_id} was not completely downloaded.")
            raise

    def get_depot_info(self, depot_id, app_id, manifest_id, branch):
        if self.steam3 is not None and app_id != INVALID_APP_ID:
            self.steam3.request_app_info(app_id)

        if not self.account_has_access(app_id, depot_id):
            print(f"Depot {depot_id} is not available from this account.")

            return None

        if manifest_id == INVALID_MANIFEST_ID:
            manifest_id = self.get_steam3_depot_manifest(depot_id, app_id, branch)

            if manifest_id == INVALID_MANIFEST_ID and branch.lower() != DEFAULT_BRANCH:
                print(
                    f'Warning: Depot {depot_id} does not have branch named "{branch}". Trying {DEFAULT_BRANCH} branch.'
                )
                branch = DEFAULT_BRANCH
                manifest_id = self.get_steam3_depot_manifest(depot_id, app_id, branch)

            if manifest_id == INVALID_MANIFEST_ID:
                print(f"Depot {depot_id} missing public subsection or manifest section.")
                return None

        self.steam3.request_depot_key(depot_id, app_id)
        depot_key = self.steam3.depot_keys.get(depot_id)

        if depot_key is None:
            print(f"No valid depot key for {depot_id}, unable to download.")
            return None

        u_version = self.get_steam3_app_build_number(app_id, branch)

        created, install_dir = self.create_directories(depot_id, u_version)

        if not created:
            print("Error: Unable to create install directories!")
            return None

        # For depots that are proxied through depotfromapp, we still need to resolve the proxy app id,
        # unless the app is freetodownload
        containing_app_id = app_id
        proxy_app_id = self.get_steam3_depot_proxy_app_id(depot_id, app_id)

        if proxy_app_id != INVALID_APP_ID:
            common = self.get_steam3_app_section(app_id, "common")

            if common is None or not as_boolean(kv_get(common, "FreeToDownload")):
                containing_app_id = proxy_app_id

        return DepotDownloadInfo(depot_id, containing_app_id, manifest_id, branch, install_dir, depot_key)

    def download_steam3(self, depots):
        ansi.progress_state(ansi.ProgressState.INDETERMINATE)

        self.cdn_pool.update_server_list()

        cts = CancellationTokenSource()
        download_counter = GlobalDownloadCounter()
        depots_to_download = []
        all_file_names_all_depots = set()

        # First, fetch all the manifests for each depot (including previous manifests) and perform the initial setup
        for depot in depots:
            depot_file_data = self.process_depot_manifest_and_files(cts, depot, download_counter)

            if depot_file_data is not None:
                depots_to_download.append(depot_file_data)
                all_file_names_all_depots.update(depot_file_data.all_file_names)

            cts.throw_if_cancellation_requested()

        # If we're about to write all the files to the same directory, we will need to first de-duplicate any files by path
        # This is in last-depot-wins order, from Steam or the list of depots supplied by the user
        if self.config.install_directory and self.config.install_directory.strip() and depots_to_download:
            claimed_file_names = set()

            for depot_file_data in reversed(depots_to_download):
                # For each depot, remove all files from the list that have been claimed by a later depot
                depot_file_data.filtered_files = [
                    file for file in depot_file_data.filtered_files if file.file_name not in claimed_file_names
                ]

                claimed_file_names.update(depot_file_data.all_file_names)

        # One set of workers downloads the chunks of every depot. Files are planned depot by depot on a few
        # threads, and their chunks are queued as soon as they are known, so the network stays busy while
        # later files are still being validated or allocated.
        chunk_queue = Queue()
        failures = []

        def chunk_worker():
            while True:
                item = chunk_queue.get()

                if item is None:
                    return

                try:
                    self.download_steam3_depot_file_chunk(cts, download_counter, *item)
                except Exception as ex:
                    failures.append(ex)
                    cts.cancel()

        workers = [gevent.spawn(chunk_worker) for _ in range(self.config.max_downloads)]
        planning_pool = ThreadPool(FILE_PLANNING_THREADS)

        try:
            for depot_file_data in depots_to_download:
                self.queue_steam3_depot_files(cts, download_counter, depot_file_data, planning_pool, chunk_queue)
        except BaseException:
            cts.cancel()
            raise
        finally:
            for _ in workers:
                chunk_queue.put(None)

            gevent.joinall(workers)
            planning_pool.kill()

        if failures:
            raise failures[0]

        for depot_file_data in depots_to_download:
            self.finish_steam3_depot_files(depot_file_data, all_file_names_all_depots)

        ansi.progress_state(ansi.ProgressState.HIDDEN)

        print(
            f"Total downloaded: {download_counter.total_bytes_compressed} bytes "
            f"({download_counter.total_bytes_uncompressed} bytes uncompressed) from {len(depots)} depots"
        )

    def process_depot_manifest_and_files(self, cts, depot, download_counter):
        depot_counter = DepotDownloadCounter()

        print(f"Processing depot {depot.depot_id}")

        old_manifest = None
        new_manifest = None
        config_dir = os.path.join(depot.install_dir, CONFIG_DIR)

        last_manifest_id = self.depot_config.installed_manifest_ids.get(depot.depot_id, INVALID_MANIFEST_ID)

        # In case we have an early exit, this will force equiv of verifyall next run.
        self.depot_config.installed_manifest_ids[depot.depot_id] = INVALID_MANIFEST_ID
        self.depot_config.save()

        if last_manifest_id != INVALID_MANIFEST_ID:
            # We only have to show this warning if the old manifest ID was different
            bad_hash_warning = last_manifest_id != depot.manifest_id
            old_manifest = util.load_manifest_from_file(config_dir, depot.depot_id, last_manifest_id, bad_hash_warning)

        if last_manifest_id == depot.manifest_id and old_manifest is not None:
            new_manifest = old_manifest
            print(f"Already have manifest {depot.manifest_id} for depot {depot.depot_id}.")
        else:
            new_manifest = util.load_manifest_from_file(config_dir, depot.depot_id, depot.manifest_id, True)

            if new_manifest is not None:
                print(f"Already have manifest {depot.manifest_id} for depot {depot.depot_id}.")
            else:
                print(f"Downloading depot {depot.depot_id} manifest")

                manifest_request_code = 0
                manifest_request_code_expiration = datetime.min

                def fetch_manifest(connection, cdn_token):
                    nonlocal manifest_request_code, manifest_request_code_expiration

                    now = datetime.now()

                    # In order to download this manifest, we need the current manifest request code
                    # The manifest request code is only valid for a specific period in time
                    if manifest_request_code == 0 or now >= manifest_request_code_expiration:
                        manifest_request_code = self.steam3.get_depot_manifest_request_code(
                            depot.depot_id, depot.app_id, depot.manifest_id, depot.branch
                        )
                        # This code will hopefully be valid for one period following the issuing period
                        manifest_request_code_expiration = now + timedelta(minutes=5)

                        # If we could not get the manifest code, this is a fatal error
                        if manifest_request_code == 0:
                            cts.cancel()

                    _LOG.debug(
                        "Downloading manifest %s from %s with %s",
                        depot.manifest_id,
                        connection,
                        self.cdn_pool.proxy_server if self.cdn_pool.proxy_server is not None else "no proxy",
                    )
                    return self.cdn_pool.cdn_client.download_manifest(
                        depot.depot_id,
                        depot.manifest_id,
                        manifest_request_code,
                        connection,
                        depot.depot_key,
                        self.cdn_pool.proxy_server,
                        cdn_token,
                    )

                new_manifest = self.download_from_cdn(
                    cts,
                    depot,
                    f"depot manifest {depot.depot_id} {depot.manifest_id}",
                    fetch_manifest,
                    abort_on_404=True,
                )

                if new_manifest is None:
                    print(f"\nUnable to download manifest {depot.manifest_id} for depot {depot.depot_id}")
                    cts.cancel()

                # Throw the cancellation exception if requested so that this task is marked failed
                cts.throw_if_cancellation_requested()

                util.save_manifest_to_file(config_dir, new_manifest)

        print(f"Manifest {depot.manifest_id} ({format_creation_time(new_manifest)})")

        if self.config.download_manifest_only:
            self.dump_manifest_to_text_file(depot, new_manifest)
            return None

        staging_dir = os.path.join(depot.install_dir, STAGING_DIR)

        files_after_exclusions = [
            file for file in manifest_files(new_manifest) if self.test_is_file_included(file.file_name)
        ]
        all_file_names = set()

        # Pre-process
        for file in files_after_exclusions:
            all_file_names.add(file.file_name)

            file_final_path = os.path.join(depot.install_dir, file.file_name)
            file_staging_path = os.path.join(staging_dir, file.file_name)

            if file.flags & EDepotFileFlag.Directory:
                os.makedirs(file_final_path, exist_ok=True)
                os.makedirs(file_staging_path, exist_ok=True)
            else:
                # Some manifests don't explicitly include all necessary directories
                os.makedirs(os.path.dirname(file_final_path), exist_ok=True)
                os.makedirs(os.path.dirname(file_staging_path), exist_ok=True)

                download_counter.complete_download_size += file.total_size
                depot_counter.complete_download_size += file.total_size

        previous_files = manifest_files(old_manifest) if old_manifest is not None else None

        depot_files_data = DepotFilesData(
            depot_download_info=depot,
            depot_counter=depot_counter,
            staging_dir=staging_dir,
            manifest=new_manifest,
            previous_manifest=old_manifest,
            filtered_files=files_after_exclusions,
            all_file_names=all_file_names,
            previous_files=previous_files,
            previous_files_by_name={file.file_name: file for file in previous_files}
            if previous_files is not None
            else None,
        )

        return depot_files_data

    def queue_steam3_depot_files(self, cts, download_counter, depot_files_data, planning_pool, chunk_queue):
        depot = depot_files_data.depot_download_info
        depot_counter = depot_files_data.depot_counter

        print(f"Downloading depot {depot.depot_id}")

        files = [file for file in depot_files_data.filtered_files if not file.flags & EDepotFileFlag.Directory]

        def process_file(file):
            needed_chunks, size_on_disk = util.run_in_thread(
                planning_pool, self.plan_steam3_depot_file, cts, depot_files_data, file
            )

            if size_on_disk is not None:
                with depot_counter.lock:
                    depot_counter.size_downloaded += size_on_disk

                    if not needed_chunks:
                        file_final_path = os.path.join(depot.install_dir, file.file_name)
                        print(
                            f"{format_percent(depot_counter.size_downloaded, depot_counter.complete_download_size)} "
                            f"{file_final_path}"
                        )

                with download_counter.lock:
                    download_counter.complete_download_size -= size_on_disk

            if not needed_chunks:
                return

            file_stream_data = FileStreamData(len(needed_chunks))

            for chunk in needed_chunks:
                chunk_queue.put((depot_files_data, file, file_stream_data, chunk))

        pool = Pool(FILE_PLANNING_THREADS)

        for _ in pool.imap_unordered(process_file, files):
            pass

    def finish_steam3_depot_files(self, depot_files_data, all_file_names_all_depots):
        depot = depot_files_data.depot_download_info
        depot_counter = depot_files_data.depot_counter

        # Check for deleted files if updating the depot.
        if depot_files_data.previous_manifest is not None:
            previous_filtered_files = {
                file.file_name for file in depot_files_data.previous_files if self.test_is_file_included(file.file_name)
            }

            # Check if we are writing to a single output directory. If not, each depot folder is managed independently
            if not self.config.install_directory or not self.config.install_directory.strip():
                # Of the list of files in the previous manifest, remove any file names that exist in the current set of
                # all file names
                previous_filtered_files -= depot_files_data.all_file_names
            else:
                # Of the list of files in the previous manifest, remove any file names that exist in the current set of
                # all file names across all depots being downloaded
                previous_filtered_files -= all_file_names_all_depots

            for existing_file_name in previous_filtered_files:
                file_final_path = os.path.join(depot.install_dir, existing_file_name)

                if not os.path.isfile(file_final_path):
                    continue

                os.remove(file_final_path)
                print(f"Deleted {file_final_path}")

        self.depot_config.installed_manifest_ids[depot.depot_id] = depot.manifest_id
        self.depot_config.save()

        print(
            f"Depot {depot.depot_id} - Downloaded {depot_counter.depot_bytes_compressed} bytes "
            f"({depot_counter.depot_bytes_uncompressed} bytes uncompressed)"
        )

    def plan_steam3_depot_file(self, cts, depot_files_data, file):
        """Runs on a worker thread: prepares the file on disk and works out which of its chunks still have to be
        downloaded. Returns (needed_chunks, size_on_disk), where size_on_disk is how many bytes of the file are
        already in place, or None for a file that was newly created. Touches no shared state."""
        cts.throw_if_cancellation_requested()

        depot = depot_files_data.depot_download_info
        staging_dir = depot_files_data.staging_dir
        old_manifest_file = None
        size_on_disk = None

        if depot_files_data.previous_files_by_name is not None:
            old_manifest_file = depot_files_data.previous_files_by_name.get(file.file_name)

        file_final_path = os.path.join(depot.install_dir, file.file_name)
        file_staging_path = os.path.join(staging_dir, file.file_name)

        # This may still exist if the previous run exited before cleanup
        if os.path.exists(file_staging_path):
            os.remove(file_staging_path)

        file_did_exist = os.path.isfile(file_final_path)

        if not file_did_exist:
            print(f"Pre-allocating {file_final_path}")

            # create new file. need all chunks
            try:
                with open(file_final_path, "wb") as fs:
                    fs.truncate(file.total_size)
            except OSError as ex:
                raise ContentDownloaderException(f"Failed to allocate file {file_final_path}: {ex}") from ex

            needed_chunks = list(file.chunks)
        else:
            # open existing
            if old_manifest_file is not None:
                needed_chunks = []

                hash_matches = old_manifest_file.file_hash == file.file_hash

                if self.config.verify_all or not hash_matches:
                    # we have a version of this file, but it doesn't fully match what we want
                    if self.config.verify_all:
                        print(f"Validating {file_final_path}")

                    matching_chunks = []
                    old_chunks = {chunk.sha: chunk for chunk in old_manifest_file.chunks}

                    for chunk in file.chunks:
                        old_chunk = old_chunks.get(chunk.sha)

                        if old_chunk is not None:
                            matching_chunks.append((old_chunk, chunk))
                        else:
                            needed_chunks.append(chunk)

                    ordered_chunks = sorted(matching_chunks, key=lambda match: match[0].offset)

                    copy_chunks = []

                    with open(file_final_path, "rb") as fs_old:
                        for old_chunk, new_chunk in ordered_chunks:
                            fs_old.seek(old_chunk.offset, os.SEEK_SET)

                            if util.adler_hash(fs_old.read(old_chunk.cb_original)) != old_chunk.crc:
                                needed_chunks.append(new_chunk)
                            else:
                                copy_chunks.append((old_chunk, new_chunk))

                    if not hash_matches or needed_chunks:
                        shutil.move(file_final_path, file_staging_path)

                        with open(file_staging_path, "rb") as fs_old:
                            try:
                                fs = open(file_final_path, "wb")
                            except OSError as ex:
                                raise ContentDownloaderException(
                                    f"Failed to resize file to expected size {file_final_path}: {ex}"
                                ) from ex

                            with fs:
                                try:
                                    fs.truncate(file.total_size)
                                except OSError as ex:
                                    raise ContentDownloaderException(
                                        f"Failed to resize file to expected size {file_final_path}: {ex}"
                                    ) from ex

                                for old_chunk, new_chunk in copy_chunks:
                                    fs_old.seek(old_chunk.offset, os.SEEK_SET)

                                    tmp = fs_old.read(old_chunk.cb_original)

                                    fs.seek(new_chunk.offset, os.SEEK_SET)
                                    fs.write(tmp)

                        os.remove(file_staging_path)
            else:
                # No old manifest or file not in old manifest. We must validate.
                with open(file_final_path, "r+b") as fs:
                    if os.path.getsize(file_final_path) != file.total_size:
                        try:
                            fs.truncate(file.total_size)
                        except OSError as ex:
                            raise ContentDownloaderException(f"Failed to allocate file {file_final_path}: {ex}") from ex

                    print(f"Validating {file_final_path}")
                    needed_chunks = util.validate_steam3_file_checksums(
                        fs, sorted(file.chunks, key=lambda chunk: chunk.offset)
                    )

            if not needed_chunks:
                return needed_chunks, file.total_size

            size_on_disk = file.total_size - sum(chunk.cb_original for chunk in needed_chunks)

        file_is_executable = bool(file.flags & EDepotFileFlag.Executable)

        if file_is_executable and (
            not file_did_exist or old_manifest_file is None or not old_manifest_file.flags & EDepotFileFlag.Executable
        ):
            platform_utilities.set_executable(file_final_path, True)
        elif (
            not file_is_executable
            and old_manifest_file is not None
            and old_manifest_file.flags & EDepotFileFlag.Executable
        ):
            platform_utilities.set_executable(file_final_path, False)

        return needed_chunks, size_on_disk

    def download_steam3_depot_file_chunk(self, cts, download_counter, depot_files_data, file, file_stream_data, chunk):
        cts.throw_if_cancellation_requested()

        depot = depot_files_data.depot_download_info
        depot_download_counter = depot_files_data.depot_counter

        chunk_id = chunk.sha.hex()

        def fetch_chunk(connection, cdn_token):
            _LOG.debug(
                "Downloading chunk %s from %s with %s",
                chunk_id,
                connection,
                self.cdn_pool.proxy_server if self.cdn_pool.proxy_server is not None else "no proxy",
            )
            return self.cdn_pool.cdn_client.download_depot_chunk(
                depot.depot_id,
                chunk,
                connection,
                depot.depot_key,
                self.cdn_pool.proxy_server,
                cdn_token,
            )

        chunk_data = self.download_from_cdn(cts, depot, f"chunk {chunk_id}", fetch_chunk)
        written = len(chunk_data) if chunk_data else 0

        if written == 0:
            print(f"Failed to find any server with chunk {chunk_id} for depot {depot.depot_id}. Aborting.")
            cts.cancel()

        # Throw the cancellation exception if requested so that this task is marked failed
        cts.throw_if_cancellation_requested()

        file_stream_data.file_lock.acquire()

        try:
            if file_stream_data.file_stream is None:
                file_final_path = os.path.join(depot.install_dir, file.file_name)
                file_stream_data.file_stream = open(file_final_path, "r+b")

            file_stream_data.file_stream.seek(chunk.offset, os.SEEK_SET)
            file_stream_data.file_stream.write(chunk_data)
        finally:
            file_stream_data.file_lock.release()

        file_stream_data.chunks_to_download -= 1
        remaining_chunks = file_stream_data.chunks_to_download

        if remaining_chunks == 0 and file_stream_data.file_stream is not None:
            file_stream_data.file_stream.close()
            file_stream_data.file_stream = None

        with depot_download_counter.lock:
            depot_download_counter.size_downloaded += written
            size_downloaded = depot_download_counter.size_downloaded
            depot_download_counter.depot_bytes_compressed += chunk.cb_compressed
            depot_download_counter.depot_bytes_uncompressed += chunk.cb_original

        with download_counter.lock:
            download_counter.total_bytes_compressed += chunk.cb_compressed
            download_counter.total_bytes_uncompressed += chunk.cb_original

            ansi.progress(download_counter.total_bytes_uncompressed, download_counter.complete_download_size)

        if remaining_chunks == 0:
            file_final_path = os.path.join(depot.install_dir, file.file_name)
            print(f"{format_percent(size_downloaded, depot_download_counter.complete_download_size)} {file_final_path}")

    def download_from_cdn(self, cts, depot, description, fetch, abort_on_404=False):
        """Calls fetch(server, cdn_auth_token) until a server answers, moving on to other servers when one
        fails. Returns None when the request should not be retried anywhere."""
        while True:
            cts.throw_if_cancellation_requested()

            connection = None

            try:
                connection = self.cdn_pool.get_connection()

                cdn_token = self.steam3.get_cdn_auth_token(depot.depot_id, connection.host)

                result = fetch(connection, cdn_token)

                self.cdn_pool.return_connection(connection)

                return result
            except TimeoutError:
                print(f"Connection timeout downloading {description}. Retrying.")
                self.cdn_pool.return_broken_connection(connection)
            except cdn_client.SteamKitWebRequestException as ex:
                # If the CDN returned 403, attempt to get a cdn auth if we didn't yet
                if ex.status_code == 403 and (depot.depot_id, connection.host) not in self.steam3.cdn_auth_tokens:
                    self.steam3.request_cdn_auth_token(depot.app_id, depot.depot_id, connection)

                    self.cdn_pool.return_connection(connection)

                    continue

                self.cdn_pool.return_broken_connection(connection)

                if ex.status_code in (401, 403) or (abort_on_404 and ex.status_code == 404):
                    print(f"Encountered {ex.status_code} for {description}. Aborting.")
                    return None

                print(f"Encountered error downloading {description}: {ex.status_code}")
            except OperationCancelledException:
                self.cdn_pool.return_connection(connection)
                return None
            except Exception as ex:
                self.cdn_pool.return_broken_connection(connection)
                print(f"Encountered unexpected error downloading {description}: {ex}")

    def dump_manifest_to_text_file(self, depot, manifest):
        txt_manifest = os.path.join(depot.install_dir, f"manifest_{depot.depot_id}_{depot.manifest_id}.txt")

        files = manifest_files(manifest)

        with open(txt_manifest, "w", encoding="utf-8") as sw:
            sw.write(f"Content Manifest for Depot {depot.depot_id} \n")
            sw.write("\n")
            sw.write(f"Manifest ID / date     : {depot.manifest_id} / {format_creation_time(manifest)} \n")

            unique_chunks = set()

            for file in files:
                for chunk in file.chunks:
                    unique_chunks.add(chunk.sha)

            sw.write(f"Total number of files  : {len(files)} \n")
            sw.write(f"Total number of chunks : {len(unique_chunks)} \n")
            sw.write(f"Total bytes on disk    : {manifest.metadata.cb_disk_original} \n")
            sw.write(f"Total bytes compressed : {manifest.metadata.cb_disk_compressed} \n")
            sw.write("\n")
            sw.write("\n")
            sw.write("          Size Chunks File SHA                                 Flags Name\n")

            for file in files:
                sha1_hash = file.file_hash.hex()
                sw.write(f"{file.total_size:14d} {len(file.chunks):6d} {sha1_hash} {file.flags:5x} {file.file_name}\n")

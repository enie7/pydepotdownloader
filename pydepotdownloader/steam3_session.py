# This file is subject to the terms and conditions defined
# in file 'LICENSE', which is part of this source code package.

import logging
import socket
import sys
from dataclasses import dataclass

import gevent
from gevent.event import AsyncResult
from steam.client import SteamClient
from steam.core.msg import MsgProto
from steam.enums import EResult, EType
from steam.enums.emsg import EMsg
from steam.steamid import SteamID

from . import ansi, steam_auth
from .cdn_client_pool import Server
from .console_authenticator import ConsoleAuthenticator
from .errors import ContentDownloaderException

_LOG = logging.getLogger("Steam3Session")

PROTOCOL_VERSION = 65580
CLIENT_PACKAGE_VERSION = 1771


@dataclass
class LogOnDetails:
    username: str = None
    password: str = None
    should_remember_password: bool = False
    access_token: str = None
    login_id: int = None
    auth_code: str = None
    two_factor_code: str = None


class Steam3Session:
    def __init__(self, details, config, account_settings):
        self.logon_details = details
        self.config = config
        self.account_settings = account_settings
        self.authenticated_user = details.username is not None or config.use_qr_code

        self.is_logged_on = False
        self.licenses = None

        self.app_tokens = {}
        self.package_tokens = {}
        self.depot_keys = {}
        self.cdn_auth_tokens = {}
        self.app_info = {}
        self.package_info = {}
        self.app_beta_passwords = {}

        self.steam_client = SteamClient()
        self.authentication = steam_auth.SteamAuthentication()

        self.b_aborted = False
        self.connection_backoff = 0
        self._needs_retry_message = False
        self._license_list_received = gevent.event.Event()

        self.steam_client.on(EMsg.ClientLicenseList, self._license_list_callback)
        self.steam_client.on(self.steam_client.EVENT_DISCONNECTED, self._disconnected_callback)

        sys.stdout.write("Connecting to Steam3...")
        sys.stdout.flush()

        self.connect()

    # region connection handling

    @property
    def steam_id(self):
        steam_id = self.steam_client.steam_id

        return steam_id if steam_id and steam_id.as_64 != 0 else None

    def connect(self):
        while not self.b_aborted:
            if not self.steam_client.connect(retry=1):
                self.connection_backoff += 1

                if self.connection_backoff >= 10:
                    print("Could not connect to Steam after 10 tries")
                    self._abort(False)
                    return

                print(f"Connection to Steam failed. Trying again (#{self.connection_backoff})...")
                gevent.sleep(self.connection_backoff)
                continue

            print(" Done!")

            # Update our tracking so that we don't time out, even if we need to reconnect multiple times,
            # e.g. if the authentication phase takes a while and therefore multiple connections.
            self.connection_backoff = 0

            if not self.steam_client.channel_secured:
                self.steam_client.wait_event(self.steam_client.EVENT_CHANNEL_SECURED, timeout=10)

            if self._log_on():
                return

            if self.b_aborted:
                return

            if self._needs_retry_message:
                self._needs_retry_message = False

                sys.stdout.write("Retrying Steam3 connection...")
                sys.stdout.flush()

            self._disconnect_client()

    def _log_on(self):
        """Returns True once logged on, False when the connection should be retried."""
        if not self.authenticated_user:
            sys.stdout.write("Logging anonymously into Steam3...")
            sys.stdout.flush()

            result = self.steam_client.anonymous_login()
        else:
            if self.logon_details.username is not None:
                print(f"Logging '{self.logon_details.username}' into Steam3...")

            if self.logon_details.access_token is None and not self._begin_auth_session():
                return False

            result = self._log_on_with_access_token()

        return self._handle_log_on_result(result)

    def _begin_auth_session(self):
        auth_session = None

        if (
            self.logon_details.username is not None
            and self.logon_details.password is not None
            and self.logon_details.access_token is None
        ):
            try:
                auth_session = self.authentication.begin_auth_session_via_credentials(
                    steam_auth.AuthSessionDetails(
                        device_friendly_name="DepotDownloader",
                        username=self.logon_details.username,
                        password=self.logon_details.password,
                        is_persistent_session=self.config.remember_password,
                        guard_data=self.account_settings.guard_data.get(self.logon_details.username),
                        authenticator=ConsoleAuthenticator(self.config.skip_app_confirmation),
                    )
                )
            except Exception as ex:
                print(f"Failed to authenticate with Steam: {ex}", file=sys.stderr)
                self._abort(False)
                return False
        elif self.logon_details.access_token is None and self.config.use_qr_code:
            print("Logging in with QR code...")

            try:
                auth_session = self.authentication.begin_auth_session_via_qr(
                    steam_auth.AuthSessionDetails(
                        device_friendly_name="DepotDownloader",
                        is_persistent_session=self.config.remember_password,
                    )
                )

                # Steam will periodically refresh the challenge url, so we need a new QR code.
                def challenge_url_changed():
                    print()
                    print("The QR code has changed:")

                    display_qr_code(auth_session.challenge_url)

                auth_session.challenge_url_changed = challenge_url_changed

                # Draw initial QR code immediately
                display_qr_code(auth_session.challenge_url)
            except Exception as ex:
                print(f"Failed to authenticate with Steam: {ex}", file=sys.stderr)
                self._abort(False)
                return False

        if auth_session is None:
            print(
                "Failed to authenticate with Steam: logging in requires a username and password or an access token.",
                file=sys.stderr,
            )
            self._abort(False)
            return False

        if auth_session is not None:
            try:
                result = auth_session.polling_wait_for_result()

                self.logon_details.username = result.account_name
                self.logon_details.password = None
                self.logon_details.access_token = result.refresh_token

                if result.new_guard_data is not None:
                    self.account_settings.guard_data[result.account_name] = result.new_guard_data

                    if self.config.use_qr_code:
                        print(
                            f"Success! Next time you can login with -username {result.account_name} "
                            "-remember-password instead of -qr."
                        )
                else:
                    self.account_settings.guard_data.pop(result.account_name, None)

                self.account_settings.login_tokens[result.account_name] = result.refresh_token
                self.account_settings.save()
            except Exception as ex:
                print(f"Failed to authenticate with Steam: {ex}", file=sys.stderr)
                self._abort(False)
                return False

        return True

    def _log_on_with_access_token(self):
        self.steam_client.username = self.logon_details.username

        message = MsgProto(EMsg.ClientLogon)
        message.header.steamid = SteamID(type="Individual", universe="Public")
        message.body.protocol_version = PROTOCOL_VERSION
        message.body.client_package_version = CLIENT_PACKAGE_VERSION
        # Steam's EOSType values for macOS and linux are negative, and the logon field is a
        # uint32, so wrap them the way SteamKit's unchecked (uint) cast does
        message.body.client_os_type = int(steam_auth.get_os_type()) & 0xFFFFFFFF
        message.body.client_language = "english"
        message.body.machine_name = f"{socket.gethostname()} (DepotDownloader)"
        message.body.should_remember_password = self.logon_details.should_remember_password
        message.body.supports_rate_limit_response = True
        message.body.obfuscated_private_ip.v4 = self.logon_details.login_id or 0
        message.body.cell_id = self.config.cell_id & 0xFFFFFFFF
        message.body.account_name = self.logon_details.username
        message.body.access_token = self.logon_details.access_token

        if self.logon_details.auth_code:
            message.body.auth_code = self.logon_details.auth_code
        if self.logon_details.two_factor_code:
            message.body.two_factor_code = self.logon_details.two_factor_code

        self.steam_client.send(message)

        response = self.steam_client.wait_msg(EMsg.ClientLogOnResponse, timeout=30)

        if response is None:
            return None

        return EResult(response.body.eresult)

    def _handle_log_on_result(self, result):
        """Port of the LogOnCallback handler. Returns True when logged on."""
        if result is None:
            print("Unable to login to Steam3: Timeout")
            self._abort(False)
            return False

        result = EResult(result)

        is_steam_guard = result == EResult.AccountLogonDenied
        is_2fa = result == EResult.AccountLoginDeniedNeedTwoFactor
        is_access_token = (
            self.config.remember_password
            and self.logon_details.access_token is not None
            and result
            in (
                EResult.InvalidPassword,
                EResult.InvalidSignature,
                EResult.AccessDenied,
                EResult.Expired,
                EResult.Revoked,
            )
        )

        if is_steam_guard or is_2fa or is_access_token:
            if not is_access_token:
                print("This account is protected by Steam Guard.")

            if is_2fa:
                while True:
                    sys.stdout.write("Please enter your 2 factor auth code from your authenticator app: ")
                    sys.stdout.flush()
                    self.logon_details.two_factor_code = sys.stdin.readline().strip()

                    if self.logon_details.two_factor_code:
                        break
            elif is_access_token:
                self.account_settings.login_tokens.pop(self.logon_details.username, None)
                self.account_settings.save()

                # TODO: Handle gracefully by falling back to password prompt?
                print(f"Access token was rejected ({result.name}).")
                self._abort(False)
                return False
            else:
                while True:
                    sys.stdout.write("Please enter the authentication code sent to your email address: ")
                    sys.stdout.flush()
                    self.logon_details.auth_code = sys.stdin.readline().strip()

                    if self.logon_details.auth_code:
                        break

            self._needs_retry_message = True

            return False

        if result == EResult.TryAnotherCM:
            sys.stdout.write("Retrying Steam3 connection (TryAnotherCM)...")
            sys.stdout.flush()
            self._disconnect_client()

            return False

        if result == EResult.ServiceUnavailable:
            print(f"Unable to login to Steam3: {result.name}")
            self._abort(False)

            return False

        if result != EResult.OK:
            print(f"Unable to login to Steam3: {result.name}")
            self._abort()

            return False

        print(" Done!")

        self.is_logged_on = True

        if self.config.cell_id == 0:
            print(f"Using Steam3 suggested CellID: {self.steam_client.cell_id}")
            self.config.cell_id = self.steam_client.cell_id

        if self.authenticated_user:
            # The license list arrives shortly after the logon response
            self._license_list_received.wait(timeout=10)

        return True

    def wait_for_credentials(self):
        return self.is_logged_on

    def _license_list_callback(self, message):
        result = EResult(message.body.eresult)

        if result != EResult.OK:
            print(f"Unable to get license list: {result.name} ")
            self._abort()

            return

        print(f"Got {len(message.body.licenses)} licenses for account!")
        self.licenses = list(message.body.licenses)

        for license in message.body.licenses:
            if license.access_token > 0:
                self.package_tokens.setdefault(license.package_id, license.access_token)

        self._license_list_received.set()

    def _disconnected_callback(self):
        if self.b_aborted or not self.is_logged_on:
            return

        self.is_logged_on = False

        print("Lost connection to Steam. Reconnecting")

        gevent.spawn(self._reconnect)

    def _reconnect(self):
        if self.b_aborted:
            return

        if not self.steam_client.connect(retry=3):
            print("Could not reconnect to Steam")
            return

        if not self.steam_client.channel_secured:
            self.steam_client.wait_event(self.steam_client.EVENT_CHANNEL_SECURED, timeout=10)

        if not self.authenticated_user:
            result = self.steam_client.anonymous_login()
            self.is_logged_on = result == EResult.OK
        else:
            self.is_logged_on = self._handle_log_on_result(self._log_on_with_access_token())

    def _abort(self, send_log_off=True):
        self.disconnect(send_log_off)

    def disconnect(self, send_log_off=True):
        if self.b_aborted:
            return

        self.b_aborted = True

        if send_log_off and self.steam_client.logged_on:
            self.steam_client.logout()

        self.is_logged_on = False
        self._disconnect_client()

        ansi.progress_state(ansi.ProgressState.HIDDEN)

    def _disconnect_client(self):
        try:
            self.steam_client.disconnect()
        except Exception:
            pass

    # endregion

    # region steam requests

    def request_app_info(self, app_id, force=False):
        if (app_id in self.app_info and not force) or self.b_aborted:
            return

        tokens = self.steam_client.get_access_tokens(app_ids=[app_id]) or {}
        token = tokens.get("apps", {}).get(app_id)

        if token:
            self.app_tokens[app_id] = token

        request = {"appid": app_id}

        if app_id in self.app_tokens:
            request["access_token"] = self.app_tokens[app_id]

        result = self.steam_client.get_product_info(apps=[request], auto_access_tokens=False)
        apps = (result or {}).get("apps", {})

        if app_id in apps:
            if apps[app_id].get("_missing_token"):
                print(f"Insufficient privileges to get access token for app {app_id}")

            print(f"Got AppInfo for {app_id}")
            self.app_info[app_id] = lowercase_keys(apps[app_id])
        else:
            self.app_info[app_id] = None

    def request_package_info(self, package_ids):
        packages = [package for package in package_ids if package not in self.package_info]

        if not packages or self.b_aborted:
            return

        requests = []

        for package in packages:
            request = {"packageid": package}

            if package in self.package_tokens:
                request["access_token"] = self.package_tokens[package]

            requests.append(request)

        result = self.steam_client.get_product_info(packages=requests, auto_access_tokens=False)
        found = (result or {}).get("packages", {})

        for package in packages:
            self.package_info[package] = lowercase_keys(found.get(package))

    def request_free_app_license(self, app_id):
        try:
            result, granted_apps, _ = self.steam_client.request_free_license([app_id])

            return app_id in (granted_apps or [])
        except Exception as ex:
            print(f"Failed to request FreeOnDemand license for app {app_id}: {ex}")
            return False

    def request_depot_key(self, depot_id, app_id=0):
        if depot_id in self.depot_keys or self.b_aborted:
            return

        depot_key = self.steam_client.get_depot_key(app_id, depot_id)

        if depot_key is None:
            print(f"Got depot key for {depot_id} result: Timeout")
            return

        result = EResult(depot_key.eresult)

        print(f"Got depot key for {depot_key.depot_id} result: {result.name}")

        if result != EResult.OK:
            return

        self.depot_keys[depot_key.depot_id] = depot_key.depot_encryption_key

    def get_depot_manifest_request_code(self, depot_id, app_id, manifest_id, branch):
        if self.b_aborted:
            return 0

        body = {"app_id": app_id, "depot_id": depot_id, "manifest_id": manifest_id}

        if branch and branch.lower() != "public":
            body["app_branch"] = branch

        response = self.steam_client.send_um_and_wait(
            "ContentServerDirectory.GetManifestRequestCode#1", body, timeout=10
        )

        request_code = response.body.manifest_request_code if response is not None else 0

        if request_code == 0:
            print(
                f"No manifest request code was returned for depot {depot_id} from app {app_id}, manifest {manifest_id}"
            )

            if not self.authenticated_user:
                print(
                    "Suggestion: Try logging in with -username as old manifests may not be available "
                    "for anonymous accounts."
                )
        else:
            print(
                f"Got manifest request code for depot {depot_id} from app {app_id}, manifest {manifest_id}, "
                f"result: {request_code}"
            )

        return request_code

    def get_servers_for_steam_pipe(self):
        response = self.steam_client.send_um_and_wait(
            "ContentServerDirectory.GetServersForSteamPipe#1", {"cell_id": self.config.cell_id}, timeout=30
        )

        if response is None:
            raise ContentDownloaderException("Failed to retrieve any download servers.")

        return [Server(info) for info in response.body.servers]

    def request_cdn_auth_token(self, app_id, depot_id, server):
        cdn_key = (depot_id, server.host)

        if self.b_aborted or cdn_key in self.cdn_auth_tokens:
            return

        completion = AsyncResult()
        self.cdn_auth_tokens[cdn_key] = completion

        _LOG.debug("Requesting CDN auth token for %s", server.host)

        response = self.steam_client.send_job_and_wait(
            MsgProto(EMsg.ClientGetCDNAuthToken),
            {"app_id": app_id, "depot_id": depot_id, "host_name": server.host},
            timeout=10,
        )

        if response is None:
            print(f"Got CDN auth token for {server.host} result: Timeout")
            # Unlike the C# version we always complete the promise, so that waiters don't hang forever
            completion.set(None)
            return

        result = EResult(response.eresult)

        print(f"Got CDN auth token for {server.host} result: {result.name} (expires {response.expiration_time})")

        completion.set(response.token if result == EResult.OK else None)

    def get_cdn_auth_token(self, depot_id, host):
        """Returns the token for an already requested (possibly still in flight) auth token."""
        promise = self.cdn_auth_tokens.get((depot_id, host))

        return promise.get() if promise is not None else None

    def check_app_beta_password(self, app_id, password):
        response = self.steam_client.send_job_and_wait(
            MsgProto(EMsg.ClientCheckAppBetaPassword), {"app_id": app_id, "betapassword": password}, timeout=10
        )

        if response is None:
            print("Retrieved 0 beta keys with result: Timeout")
            return

        print(f"Retrieved {len(response.betapasswords)} beta keys with result: {EResult(response.eresult).name}")

        for entry in response.betapasswords:
            key = bytes.fromhex(entry.betapassword)
            self.app_beta_passwords[entry.betaname] = key
            self.app_beta_passwords[entry.betaname.lower()] = key

    def get_app_beta_password(self, branch):
        key = self.app_beta_passwords.get(branch)

        return key if key is not None else self.app_beta_passwords.get(branch.lower())

    def get_published_file_details(self, app_id, pub_file):
        response = self.steam_client.send_um_and_wait(
            "PublishedFile.GetDetails#1",
            {"appid": app_id, "publishedfileids": [pub_file], "includechildren": True},
            timeout=10,
        )

        if response is None:
            raise ContentDownloaderException(f"Timeout while retrieving file details for pubfile {pub_file}.")

        result = EResult(response.header.eresult)

        if result == EResult.OK:
            details = response.body.publishedfiledetails

            return details[0] if details else None

        raise ContentDownloaderException(
            f"EResult {int(result)} ({result.name}) while retrieving file details for pubfile {pub_file}."
        )

    def get_ugc_details(self, ugc_handle):
        response = self.steam_client.send_job_and_wait(
            MsgProto(EMsg.ClientUFSGetUGCDetails), {"hcontent": ugc_handle}, timeout=10
        )

        if response is None:
            raise ContentDownloaderException(f"Timeout while retrieving UGC details for {ugc_handle}.")

        result = EResult(response.eresult)

        if result == EResult.OK:
            return response
        elif result == EResult.FileNotFound:
            return None

        raise ContentDownloaderException(
            f"EResult {int(result)} ({result.name}) while retrieving UGC details for {ugc_handle}."
        )

    def is_anonymous_account(self):
        steam_id = self.steam_id

        return steam_id is not None and steam_id.type == EType.AnonUser

    # endregion


def lowercase_keys(node):
    """KeyValues keys are case insensitive. Lower casing them once up front lets lookups be plain dict gets."""
    if isinstance(node, dict):
        return {key.lower() if isinstance(key, str) else key: lowercase_keys(value) for key, value in node.items()}

    return node


def display_qr_code(challenge_url):
    try:
        import qrcode
    except ImportError:
        print("Install the 'qrcode' package to display a QR code. Open this link instead:")
        print(challenge_url)
        return

    qr = qrcode.QRCode(error_correction=qrcode.constants.ERROR_CORRECT_L, border=4)
    qr.add_data(challenge_url)

    print("Use the Steam Mobile App to sign in with this QR code:")

    for line in _qr_ascii_lines(qr):
        print(line)


def _qr_ascii_lines(qr):
    matrix = qr.get_matrix()

    for row in matrix:
        yield "".join("██" if cell else "  " for cell in row)

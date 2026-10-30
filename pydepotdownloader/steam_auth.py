# This file is subject to the terms and conditions defined
# in file 'LICENSE', which is part of this source code package.
"""Port of SteamKit2's SteamKit2.Authentication namespace.

The Steam library this port is built on predates Steam's current authentication
scheme, so the auth session flow is implemented here against the (unauthenticated)
IAuthenticationService endpoints, exactly like SteamKit does over the CM connection.
The result is a refresh token, which is what the logon message wants.
"""

import base64
import sys

import gevent
from Cryptodome.Cipher import PKCS1_v1_5
from Cryptodome.PublicKey import RSA
from steam.enums import EOSType, EResult
from steam.protobufs import steammessages_auth_pb2 as auth_pb

from . import http_client_factory

API_BASE = "https://api.steampowered.com/IAuthenticationService/"

PLATFORM_TYPE_STEAM_CLIENT = auth_pb.k_EAuthTokenPlatformType_SteamClient

SESSION_PERSISTENCE_EPHEMERAL = 0
SESSION_PERSISTENCE_PERSISTENT = 1

GUARD_TYPE_UNKNOWN = auth_pb.k_EAuthSessionGuardType_Unknown
GUARD_TYPE_NONE = auth_pb.k_EAuthSessionGuardType_None
GUARD_TYPE_EMAIL_CODE = auth_pb.k_EAuthSessionGuardType_EmailCode
GUARD_TYPE_DEVICE_CODE = auth_pb.k_EAuthSessionGuardType_DeviceCode
GUARD_TYPE_DEVICE_CONFIRMATION = auth_pb.k_EAuthSessionGuardType_DeviceConfirmation
GUARD_TYPE_EMAIL_CONFIRMATION = auth_pb.k_EAuthSessionGuardType_EmailConfirmation
GUARD_TYPE_MACHINE_TOKEN = auth_pb.k_EAuthSessionGuardType_MachineToken

_PREFERRED_CONFIRMATION_TYPES = [
    GUARD_TYPE_NONE,
    GUARD_TYPE_DEVICE_CONFIRMATION,
    GUARD_TYPE_DEVICE_CODE,
    GUARD_TYPE_EMAIL_CODE,
    GUARD_TYPE_EMAIL_CONFIRMATION,
    GUARD_TYPE_MACHINE_TOKEN,
    GUARD_TYPE_UNKNOWN,
]


def get_os_type():
    if sys.platform == "win32":
        return EOSType.Windows10

    if sys.platform == "darwin":
        return EOSType.MacOSUnknown

    if sys.platform.startswith("linux"):
        return EOSType.LinuxUnknown

    return EOSType.Unknown


class AuthenticationException(Exception):
    def __init__(self, message, result):
        super().__init__(f"{message} ({EResult(result).name}).")
        self.result = EResult(result)


class AuthSessionDetails:
    def __init__(
        self,
        username=None,
        password=None,
        device_friendly_name="DepotDownloader",
        platform_type=PLATFORM_TYPE_STEAM_CLIENT,
        client_os_type=None,
        is_persistent_session=False,
        website_id="Client",
        guard_data=None,
        authenticator=None,
    ):
        self.username = username
        self.password = password
        self.device_friendly_name = device_friendly_name
        self.platform_type = platform_type
        self.client_os_type = get_os_type() if client_os_type is None else client_os_type
        self.is_persistent_session = is_persistent_session
        self.website_id = website_id
        self.guard_data = guard_data
        self.authenticator = authenticator


class AuthPollResult:
    def __init__(self, response):
        self.access_token = response.access_token
        self.refresh_token = response.refresh_token
        self.account_name = response.account_name
        self.new_guard_data = response.new_guard_data or None


class AuthSession:
    def __init__(self, authentication, authenticator, client_id, request_id, allowed_confirmations, interval):
        self.authentication = authentication
        self.authenticator = authenticator
        self.client_id = client_id
        self.request_id = request_id
        self.allowed_confirmations = _sort_confirmations(allowed_confirmations)
        self.polling_interval = interval

    def polling_wait_for_result(self):
        poll_loop = False
        preferred_confirmation = self.allowed_confirmations[0] if self.allowed_confirmations else None

        if preferred_confirmation is None or preferred_confirmation.confirmation_type == GUARD_TYPE_UNKNOWN:
            raise Exception("There are no allowed confirmations")

        # If an authenticator is provided and we device confirmation is available, allow consumers to choose whether
        # they want to simply poll until confirmation is accepted, or whether they want to fallback to the next
        # preferred confirmation type.
        if (
            self.authenticator is not None
            and preferred_confirmation.confirmation_type == GUARD_TYPE_DEVICE_CONFIRMATION
        ):
            if not self.authenticator.accept_device_confirmation():
                if len(self.allowed_confirmations) <= 1:
                    raise Exception(
                        "AcceptDeviceConfirmation returned false which indicates a fallback to another confirmation "
                        "type, but there are no other confirmation types available."
                    )

                preferred_confirmation = self.allowed_confirmations[1]

        confirmation_type = preferred_confirmation.confirmation_type

        if confirmation_type == GUARD_TYPE_NONE:
            pass
        elif confirmation_type in (GUARD_TYPE_EMAIL_CODE, GUARD_TYPE_DEVICE_CODE):
            if not isinstance(self, CredentialsAuthSession):
                raise Exception(
                    f"Got {confirmation_type} confirmation type in a session that is not a credentials auth session."
                )

            if self.authenticator is None:
                raise Exception("This account requires an authenticator for login, but none was provided.")

            expected_invalid_code_result = (
                EResult.InvalidLoginAuthCode
                if confirmation_type == GUARD_TYPE_EMAIL_CODE
                else EResult.TwoFactorCodeMismatch
            )
            previous_code_was_incorrect = False
            waiting_for_valid_code = True

            while waiting_for_valid_code:
                try:
                    if confirmation_type == GUARD_TYPE_EMAIL_CODE:
                        code = self.authenticator.get_email_code(
                            preferred_confirmation.associated_message, previous_code_was_incorrect
                        )
                    else:
                        code = self.authenticator.get_device_code(previous_code_was_incorrect)

                    if not code:
                        raise Exception("No code was provided by the authenticator.")

                    self.send_steam_guard_code(code, confirmation_type)

                    waiting_for_valid_code = False
                except AuthenticationException as ex:
                    if ex.result != expected_invalid_code_result:
                        raise

                    previous_code_was_incorrect = True
        elif confirmation_type == GUARD_TYPE_DEVICE_CONFIRMATION:
            # This is a prompt that appears in the Steam mobile app
            poll_loop = True
        else:
            raise Exception(f"Unsupported confirmation type {confirmation_type}.")

        if not poll_loop:
            poll_response = self.poll_auth_session_status()

            if poll_response is None:
                raise AuthenticationException("Authentication failed", EResult.Fail)

            return poll_response

        while True:
            gevent.sleep(self.polling_interval)

            poll_response = self.poll_auth_session_status()

            if poll_response is not None:
                return poll_response

    def poll_auth_session_status(self):
        request = auth_pb.CAuthentication_PollAuthSessionStatus_Request(
            client_id=self.client_id,
            request_id=self.request_id,
        )

        response = self.authentication.service_call(
            "PollAuthSessionStatus", request, auth_pb.CAuthentication_PollAuthSessionStatus_Response
        )

        self.handle_poll_auth_session_status_response(response)

        if response.refresh_token:
            return AuthPollResult(response)

        return None

    def handle_poll_auth_session_status_response(self, response):
        if response.new_client_id:
            self.client_id = response.new_client_id


class CredentialsAuthSession(AuthSession):
    def __init__(self, authentication, authenticator, response):
        super().__init__(
            authentication,
            authenticator,
            response.client_id,
            response.request_id,
            response.allowed_confirmations,
            response.interval,
        )
        self.steam_id = response.steamid

    def send_steam_guard_code(self, code, code_type):
        request = auth_pb.CAuthentication_UpdateAuthSessionWithSteamGuardCode_Request(
            client_id=self.client_id,
            steamid=self.steam_id,
            code=code,
            code_type=code_type,
        )

        # Observed results can be InvalidLoginAuthCode, TwoFactorCodeMismatch, Expired, DuplicateRequest.
        # DuplicateRequest happens when accepting the prompt in the mobile app, and then trying to send guard code
        # here, we do not throw on it here because authentication will succeed on the next poll.
        self.authentication.service_call(
            "UpdateAuthSessionWithSteamGuardCode",
            request,
            auth_pb.CAuthentication_UpdateAuthSessionWithSteamGuardCode_Response,
            allowed_results=(EResult.OK, EResult.DuplicateRequest),
            error_message="Failed to send steam guard code",
        )


class QrAuthSession(AuthSession):
    def __init__(self, authentication, authenticator, response):
        super().__init__(
            authentication,
            authenticator,
            response.client_id,
            response.request_id,
            response.allowed_confirmations,
            response.interval,
        )
        self.challenge_url = response.challenge_url
        self.challenge_url_changed = None

    def handle_poll_auth_session_status_response(self, response):
        super().handle_poll_auth_session_status_response(response)

        if response.new_challenge_url:
            self.challenge_url = response.new_challenge_url

            if self.challenge_url_changed is not None:
                self.challenge_url_changed()


class SteamAuthentication:
    def __init__(self):
        self.session = http_client_factory.create_http_client()

    def service_call(
        self, method, request, response_class, allowed_results=(EResult.OK,), error_message=None, http_method="POST"
    ):
        encoded = base64.b64encode(request.SerializeToString()).decode("ascii")
        url = f"{API_BASE}{method}/v1/"

        if http_method == "GET":
            http_response = self.session.get(url, params={"input_protobuf_encoded": encoded}, timeout=30)
        else:
            http_response = self.session.post(url, data={"input_protobuf_encoded": encoded}, timeout=30)

        result = EResult(int(http_response.headers.get("x-eresult", EResult.OK)))

        if result not in allowed_results:
            raise AuthenticationException(error_message or f"Failed to call {method}", result)

        if not http_response.ok and result == EResult.OK:
            raise AuthenticationException(
                error_message or f"Failed to call {method} (HTTP {http_response.status_code})", EResult.Fail
            )

        response = response_class()
        response.ParseFromString(http_response.content)

        return response

    def get_password_rsa_public_key(self, account_name):
        request = auth_pb.CAuthentication_GetPasswordRSAPublicKey_Request(account_name=account_name)

        return self.service_call(
            "GetPasswordRSAPublicKey",
            request,
            auth_pb.CAuthentication_GetPasswordRSAPublicKey_Response,
            error_message="Failed to get password public key",
            http_method="GET",
        )

    def begin_auth_session_via_qr(self, details):
        request = auth_pb.CAuthentication_BeginAuthSessionViaQR_Request(
            device_friendly_name=details.device_friendly_name,
            platform_type=details.platform_type,
            device_details=auth_pb.CAuthentication_DeviceDetails(
                device_friendly_name=details.device_friendly_name,
                platform_type=details.platform_type,
                os_type=int(details.client_os_type),
            ),
        )

        response = self.service_call(
            "BeginAuthSessionViaQR",
            request,
            auth_pb.CAuthentication_BeginAuthSessionViaQR_Response,
            error_message="Failed to begin QR auth session",
        )

        return QrAuthSession(self, details.authenticator, response)

    def begin_auth_session_via_credentials(self, details):
        if not details.username or not details.password:
            raise ValueError("BeginAuthSessionViaCredentials requires a username and password to be set in 'details'.")

        # Encrypt the password
        public_key = self.get_password_rsa_public_key(details.username)

        rsa = RSA.construct((int(public_key.publickey_mod, 16), int(public_key.publickey_exp, 16)))
        encrypted_password = PKCS1_v1_5.new(rsa).encrypt(details.password.encode("utf-8"))

        request = auth_pb.CAuthentication_BeginAuthSessionViaCredentials_Request(
            account_name=details.username,
            persistence=SESSION_PERSISTENCE_PERSISTENT
            if details.is_persistent_session
            else SESSION_PERSISTENCE_EPHEMERAL,
            website_id=details.website_id,
            guard_data=details.guard_data,
            encrypted_password=base64.b64encode(encrypted_password).decode("ascii"),
            encryption_timestamp=public_key.timestamp,
            device_friendly_name=details.device_friendly_name,
            platform_type=details.platform_type,
            device_details=auth_pb.CAuthentication_DeviceDetails(
                device_friendly_name=details.device_friendly_name,
                platform_type=details.platform_type,
                os_type=int(details.client_os_type),
            ),
        )

        # eresult can be InvalidPassword, ServiceUnavailable, InvalidParam, RateLimitExceeded
        response = self.service_call(
            "BeginAuthSessionViaCredentials",
            request,
            auth_pb.CAuthentication_BeginAuthSessionViaCredentials_Response,
            error_message="Authentication failed",
        )

        return CredentialsAuthSession(self, details.authenticator, response)


def _sort_confirmations(confirmations):
    sort_order = {value: index for index, value in enumerate(_PREFERRED_CONFIRMATION_TYPES)}

    return sorted(confirmations, key=lambda x: sort_order.get(x.confirmation_type, sys.maxsize))

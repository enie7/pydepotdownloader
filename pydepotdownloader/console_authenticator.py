# This file is subject to the terms and conditions defined
# in file 'LICENSE', which is part of this source code package.

import sys


# This is practically copied from https://github.com/SteamRE/SteamKit/blob/master/SteamKit2/SteamKit2/Steam/Authentication/UserConsoleAuthenticator.cs
class ConsoleAuthenticator:
    def __init__(self, skip_app_confirmation=False):
        self.skip_app_confirmation = skip_app_confirmation

    def get_device_code(self, previous_code_was_incorrect):
        if previous_code_was_incorrect:
            print("The previous 2-factor auth code you have provided is incorrect.", file=sys.stderr)

        while True:
            sys.stderr.write("STEAM GUARD! Please enter your 2-factor auth code from your authenticator app: ")
            sys.stderr.flush()
            code = sys.stdin.readline()

            if not code:
                return None

            code = code.strip()

            if code:
                return code

    def get_email_code(self, email, previous_code_was_incorrect):
        if previous_code_was_incorrect:
            print("The previous 2-factor auth code you have provided is incorrect.", file=sys.stderr)

        while True:
            sys.stderr.write(f"STEAM GUARD! Please enter the auth code sent to the email at {email}: ")
            sys.stderr.flush()
            code = sys.stdin.readline()

            if not code:
                return None

            code = code.strip()

            if code:
                return code

    def accept_device_confirmation(self):
        if self.skip_app_confirmation:
            return False

        print("STEAM GUARD! Use the Steam Mobile App to confirm your sign in...", file=sys.stderr)

        return True

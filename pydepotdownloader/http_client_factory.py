# This file is subject to the terms and conditions defined
# in file 'LICENSE', which is part of this source code package.

import socket

import requests
import urllib3.util.connection

from . import UPSTREAM_VERSION

# Same as DepotDownloader, so the CDN sees the client it already knows
USER_AGENT = f"DepotDownloader/{UPSTREAM_VERSION}"

# This is based on the dotnet issue #44686 and its workaround at
# https://github.com/dotnet/runtime/issues/44686#issuecomment-733797994
# We don't know if the IPv6 stack is functional.
urllib3.util.connection.allowed_gai_family = lambda: socket.AF_INET


def create_http_client(pool_maxsize=None):
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT

    if pool_maxsize:
        # urllib3 defaults to 10 connections per host, above which it discards and reopens them
        # on every request, so the pool has to keep up with -max-downloads
        adapter = requests.adapters.HTTPAdapter(pool_connections=pool_maxsize, pool_maxsize=pool_maxsize)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

    return session

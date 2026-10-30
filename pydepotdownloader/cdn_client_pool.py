# This file is subject to the terms and conditions defined
# in file 'LICENSE', which is part of this source code package.

from . import cdn_client
from .errors import ContentDownloaderException

# How many servers to download from at once
MAX_ACTIVE_SERVERS = 4


class Server:
    """Port of SteamKit2.CDN.Server."""

    def __init__(self, info):
        https = info.https_support == "mandatory"

        self.protocol = "https" if https else "http"
        self.host = info.host
        self.vhost = info.vhost
        self.port = 443 if https else 80

        self.type = info.type
        self.source_id = info.source_id
        self.cell_id = info.cell_id

        self.load = info.load
        self.weighted_load = info.weighted_load
        self.num_entries = info.num_entries_in_client_list
        self.steam_china_only = info.steam_china_only

        self.use_as_proxy = info.use_as_proxy
        self.proxy_request_path_template = info.proxy_request_path_template

        self.allowed_app_ids = list(info.allowed_app_ids)

    def __str__(self):
        return f"{self.host}:{self.port} ({self.type})"

    __repr__ = __str__


class CDNClientPool:
    """CDNClientPool provides a pool of connections to CDN endpoints, requesting CDN tokens as needed.

    Unlike SteamKit's pool, which sends everything to one server until it fails, requests are spread
    over the best few servers: per-host throughput is what limits a download, not the connection.
    """

    def __init__(self, steam_session, app_id, config, account_settings):
        self.steam_session = steam_session
        self.app_id = app_id
        self.account_settings = account_settings
        self.cdn_client = cdn_client.Client(config.max_downloads, config.use_lancache)
        self.proxy_server = None

        # Every eligible server, best first, and the ones currently being handed out
        self.servers = []
        self.active_servers = []
        self._next_candidate = 0
        self._in_flight = {}

    def update_server_list(self):
        servers = self.steam_session.get_servers_for_steam_pipe()

        self.proxy_server = next((x for x in servers if x.use_as_proxy), None)

        def sort_key(server):
            penalty = self.account_settings.content_server_penalty.get(server.host, 0)
            return penalty, server.weighted_load

        eligible_servers = [
            server
            for server in servers
            if (not server.allowed_app_ids or self.app_id in server.allowed_app_ids)
            and server.type in ("SteamCache", "CDN")
        ]

        if not eligible_servers:
            raise ContentDownloaderException("Failed to retrieve any download servers.")

        self.servers = sorted(eligible_servers, key=sort_key)
        self.active_servers = self.servers[:MAX_ACTIVE_SERVERS]
        self._next_candidate = len(self.active_servers)
        self._in_flight = {server: 0 for server in self.servers}

    def get_connection(self):
        # Everything runs on the gevent hub and nothing here yields, so no lock is needed.
        # Steam weights servers with num_entries (an ISP cache may count for several Valve ones).
        server = min(self.active_servers, key=lambda s: self._in_flight[s] / max(1, s.num_entries))
        self._in_flight[server] += 1

        return server

    def return_connection(self, server):
        if server is None:
            return

        self._in_flight[server] = max(0, self._in_flight[server] - 1)

    def return_broken_connection(self, server):
        if server is None:
            return

        self.return_connection(server)

        # Several requests to the same server usually fail together, only the first one rotates it out
        if server not in self.active_servers:
            return

        if len(self.servers) > len(self.active_servers):
            replacement = None

            while replacement is None:
                candidate = self.servers[self._next_candidate % len(self.servers)]
                self._next_candidate += 1

                if candidate not in self.active_servers:
                    replacement = candidate

            self.active_servers[self.active_servers.index(server)] = replacement
        elif len(self.active_servers) > 1:
            # Nothing left to swap in, keep using the others. It comes back when one of them is rotated out.
            self.active_servers.remove(server)

        # TODO: Add server to ContentServerPenalty

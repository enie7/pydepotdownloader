from types import SimpleNamespace

import pytest

from pydepotdownloader import cdn_client, content_downloader
from pydepotdownloader.account_settings_store import AccountSettingsStore
from pydepotdownloader.cdn_client_pool import MAX_ACTIVE_SERVERS, CDNClientPool
from pydepotdownloader.download_config import DownloadConfig


class FakeServer:
    def __init__(self, host, weighted_load=50, num_entries=1, server_type="SteamCache"):
        self.host = host
        self.weighted_load = weighted_load
        self.num_entries = num_entries
        self.type = server_type
        self.allowed_app_ids = []
        self.use_as_proxy = False


make_server = FakeServer


def make_pool(servers):
    session = SimpleNamespace(get_servers_for_steam_pipe=lambda: servers)
    pool = CDNClientPool(session, 730, DownloadConfig(max_downloads=4), AccountSettingsStore())
    pool.update_server_list()
    return pool


def test_requests_are_spread_over_the_best_servers():
    servers = [make_server(f"cache{i}", weighted_load=i) for i in range(10)]
    pool = make_pool(list(reversed(servers)))

    hosts = {pool.get_connection().host for _ in range(MAX_ACTIVE_SERVERS)}

    assert hosts == {f"cache{i}" for i in range(MAX_ACTIVE_SERVERS)}


def test_num_entries_weights_a_server():
    pool = make_pool([make_server("isp", weighted_load=0, num_entries=3), make_server("valve")])

    picks = [pool.get_connection().host for _ in range(8)]

    assert picks.count("isp") == 6
    assert picks.count("valve") == 2


def test_returned_connections_free_up_a_server():
    pool = make_pool([make_server("a", weighted_load=0), make_server("b", weighted_load=1)])

    first = pool.get_connection()
    pool.return_connection(first)

    assert pool.get_connection() is first


def test_broken_server_is_replaced_once():
    servers = [make_server(f"cache{i}", weighted_load=i) for i in range(MAX_ACTIVE_SERVERS + 2)]
    pool = make_pool(servers)
    broken = servers[0]

    pool.get_connection(), pool.get_connection()
    pool.return_broken_connection(broken)
    pool.return_broken_connection(broken)  # a second failure from the same burst rotates nothing more

    assert broken not in pool.active_servers
    assert set(pool.active_servers) == set(servers[1 : MAX_ACTIVE_SERVERS + 1])


def test_broken_server_is_set_aside_when_there_is_no_spare():
    a, b = make_server("a", weighted_load=0), make_server("b", weighted_load=1)
    pool = make_pool([a, b])

    pool.return_broken_connection(pool.get_connection())
    assert pool.active_servers == [b]

    # The last server is never dropped, a failure swaps the other one back in instead
    pool.return_broken_connection(pool.get_connection())
    assert pool.active_servers == [a]


def test_no_eligible_servers():
    with pytest.raises(content_downloader.ContentDownloaderException):
        make_pool([make_server("x", server_type="OpenCache")])


class FakeSteam:
    def __init__(self):
        self.cdn_auth_tokens = {}
        self.token_requests = []

    def get_cdn_auth_token(self, depot_id, host):
        return self.cdn_auth_tokens.get((depot_id, host))

    def request_cdn_auth_token(self, app_id, depot_id, server):
        self.token_requests.append(server.host)
        self.cdn_auth_tokens[(depot_id, server.host)] = "token"


@pytest.fixture
def downloader():
    downloader = content_downloader.ContentDownloader(DownloadConfig(), AccountSettingsStore())
    downloader.steam3 = FakeSteam()
    downloader.cdn_pool = make_pool([make_server("a", weighted_load=0), make_server("b", weighted_load=1)])
    return downloader


DEPOT = SimpleNamespace(depot_id=1, app_id=2)


def test_download_retries_on_other_servers(downloader, capsys):
    attempts = []

    def fetch(server, token):
        attempts.append(server.host)

        if len(attempts) == 1:
            raise TimeoutError()
        if len(attempts) == 2:
            raise cdn_client.SteamKitWebRequestException("busy", 503)

        return b"data"

    result = downloader.download_from_cdn(content_downloader.CancellationTokenSource(), DEPOT, "chunk ab", fetch)

    assert result == b"data"
    assert attempts == ["a", "b", "a"]
    assert all(count == 0 for count in downloader.cdn_pool._in_flight.values())
    assert "Connection timeout downloading chunk ab" in capsys.readouterr().out


def test_download_requests_cdn_token_on_first_403(downloader):
    tokens = []

    def fetch(server, token):
        tokens.append(token)

        if token is None:
            raise cdn_client.SteamKitWebRequestException("forbidden", 403)

        return b"data"

    result = downloader.download_from_cdn(content_downloader.CancellationTokenSource(), DEPOT, "chunk ab", fetch)

    assert result == b"data"
    assert tokens == [None, "token"]
    assert downloader.steam3.token_requests == ["a"]


@pytest.mark.parametrize(("status", "abort_on_404"), [(401, False), (403, False), (404, True)])
def test_download_gives_up_on_fatal_status(downloader, status, abort_on_404):
    downloader.steam3.cdn_auth_tokens[(1, "a")] = "token"

    def fetch(server, token):
        raise cdn_client.SteamKitWebRequestException("no", status)

    cts = content_downloader.CancellationTokenSource()
    assert downloader.download_from_cdn(cts, DEPOT, "manifest", fetch, abort_on_404=abort_on_404) is None
    assert all(count == 0 for count in downloader.cdn_pool._in_flight.values())

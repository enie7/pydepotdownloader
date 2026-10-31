# This file is subject to the terms and conditions defined
# in file 'LICENSE', which is part of this source code package.
"""Python port of SteamRE/DepotDownloader, a Steam depot downloading utility."""

# The Steam client library is built on gevent. Patching has to happen before
# anything imports socket/ssl (i.e. before any other module of this package),
# otherwise blocking socket calls would stall the greenlet running the
# connection to Steam while chunks are being downloaded.
import steam.monkey

steam.monkey.patch_minimal()

__version__ = "0.0.1"

# The SteamRE/DepotDownloader release this is a port of
UPSTREAM_VERSION = "3.4.0"

# This file is subject to the terms and conditions defined
# in file 'LICENSE', which is part of this source code package.

from dataclasses import dataclass, field

# DepotDownloader uses 8. Chunk requests are latency bound, so more of them in flight is what makes a download
# fast: in testing 48 had about twice the throughput of 16.
DEFAULT_MAX_DOWNLOADS = 48


@dataclass
class DownloadConfig:
    cell_id: int = 0
    download_all_platforms: bool = False
    download_all_archs: bool = False
    download_all_languages: bool = False
    download_manifest_only: bool = False
    install_directory: str = None

    using_file_list: bool = False
    files_to_download: set = field(default_factory=set)
    files_to_download_regex: list = field(default_factory=list)

    beta_password: str = None

    verify_all: bool = False

    max_downloads: int = DEFAULT_MAX_DOWNLOADS

    # Set when -use-lancache found a Lancache on the local network
    use_lancache: bool = False

    remember_password: bool = False

    # A Steam LoginID to allow multiple concurrent connections
    login_id: int = None

    use_qr_code: bool = False
    skip_app_confirmation: bool = False

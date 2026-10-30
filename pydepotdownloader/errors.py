# This file is subject to the terms and conditions defined
# in file 'LICENSE', which is part of this source code package.


class ContentDownloaderException(Exception):
    """A download failed in a way that is reported to the user as a plain message, without a traceback."""


class OperationCancelledException(Exception):
    pass

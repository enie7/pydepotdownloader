#!/usr/bin/env python3
# This file is subject to the terms and conditions defined
# in file 'LICENSE', which is part of this source code package.
"""Entry point script: ./depotdownloader.py -app <id> [options]"""

import sys

from pydepotdownloader.program import main

if __name__ == "__main__":
    sys.exit(main())

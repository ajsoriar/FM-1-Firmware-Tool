"""Entry point used by build.ps1 (PyInstaller); identical to `pyw -m app`."""

import sys

from app.__main__ import main

sys.exit(main())

"""让 `python -m harness` 也能启动。"""

import sys

from .cli import main

sys.exit(main())

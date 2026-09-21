"""PyInstaller entry point. Identical to ``python -m jarvis.ui``.

A thin launcher only: PyInstaller's ``Analysis`` needs a top-level script,
not a ``-m`` package invocation, so this exists to name one. The actual
bootstrap is ``jarvis.ui.app.main`` - unchanged, and run the same way whether
JARVIS is launched from source or from this packaged build.
"""

import sys

from jarvis.ui.app import main

if __name__ == "__main__":
    sys.exit(main())

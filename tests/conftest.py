"""Make the integration's pure core importable without Home Assistant.

``custom_components/multisplit`` goes on the path, so ``core`` is a top-level
package here and ``custom_components.multisplit.core`` inside Home Assistant.
The relative imports within ``core`` work unchanged either way, and the
integration's own ``__init__.py`` -- which does import Home Assistant -- is
never executed by the test suite.
"""

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parent.parent / "custom_components" / "multisplit")
)

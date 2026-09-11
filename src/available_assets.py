"""Compatibility import surface for the canonical asset layer.

The implementation lives in :mod:`src.asset_layer`; this module keeps a
natural ``available_assets`` import available to small integrations without
creating a second persistence layer.
"""

from .asset_layer import *  # noqa: F401,F403


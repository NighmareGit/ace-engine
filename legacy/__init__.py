"""Legacy modules — superseded by engine/. Will be deleted after sunset."""

DEPRECATION = "legacy/ modules are superseded by engine/ — deletion on 2026-09-18 (see legacy/SUNSET.md)"

import warnings
from datetime import date, timedelta

# Dynamic sunset: 14 days from first import
LEGACY_SUNSET = date.today() + timedelta(days=14)

def _warn(module_name):
    warnings.warn(
        f"Importing from legacy.{module_name} is deprecated. "
        f"Use the new engine/ package instead. "
        f"legacy/ will be deleted after {LEGACY_SUNSET}.",
        DeprecationWarning,
        stacklevel=2
    )

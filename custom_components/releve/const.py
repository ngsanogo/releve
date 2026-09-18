"""Constants of the releve integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final

DOMAIN: Final = "releve"

DEFAULT_URL: Final = "http://127.0.0.1:8080"
DOCS_URL: Final = "https://github.com/ngsanogo/releve"
# The cache changes a few times a day (one sync pass every few hours); reading
# it is local and cheap, so ten minutes keeps Home Assistant close behind.
UPDATE_INTERVAL: Final = timedelta(minutes=10)
REQUEST_TIMEOUT_SECONDS: Final = 15

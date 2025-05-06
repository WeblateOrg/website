from __future__ import annotations

import datetime
import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import ClassVar

import requests

logger = logging.getLogger(__name__)
RATE_URL = (
    "https://www.cnb.cz/cs/financni_trhy/devizovy_trh/"
    "kurzy_devizoveho_trhu/denni_kurz.txt?date={}"
)
CACHE_DIR = Path.home() / ".cache" / "fakturace"


class DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return str(o)
        return super().default(o)


class InvalidDataError(Exception):
    pass


class UncachedExchangeRates:
    @classmethod
    def download(cls, date: str) -> dict[str, Decimal]:
        result: dict[str, Decimal] = {}
        parts = date.split("-")
        date_fmt = f"{parts[2]}.{parts[1]}.{parts[0]}"
        response = requests.get(RATE_URL.format(date_fmt), timeout=10)
        response.raise_for_status()
        text = response.text
        # Check header
        if not text.startswith(date_fmt):
            raise InvalidDataError(text)
        for line in response.text.splitlines():
            if "|" not in line:
                continue
            parts = line.split("|")
            if parts[4] in {"kurz", "Rate"}:
                continue
            result[parts[3]] = Decimal(parts[4].replace(",", "."))

        return result

    @classmethod
    def get(cls, currency: str, date: datetime.date, *, recursion: int = 0) -> Decimal:
        date_iso = date.isoformat()
        if currency == "CZK":
            return Decimal(1)
        try:
            rates = cls.download(date_iso)
        except (requests.RequestException, InvalidDataError):
            logger.exception("failed to fetch exchange rate data")
            if recursion > 5:
                raise
            # Fallback on previous day if data is not available
            return cls.get(
                currency, date - datetime.timedelta(days=1), recursion=recursion + 1
            )
        return rates[currency]


class ExchangeRates(UncachedExchangeRates):
    datacache: ClassVar[dict[str, dict[str, Decimal]]] = {}

    @classmethod
    def download(cls, date: str) -> dict[str, Decimal]:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file = CACHE_DIR / f"rates-{date}"

        # Filesystem cache
        if date not in cls.datacache and cache_file.exists():
            cached: dict[str, str | float] = json.loads(cache_file.read_text())
            # Convert str (from DecimalEncoder) or float (legacy cache) to Decimal
            cls.datacache[date] = {key: Decimal(value) for key, value in cached.items()}

        # Load remotely
        if date not in cls.datacache:
            cls.datacache[date] = super().download(date)

            # Update filesystem cache
            cache_file.write_text(json.dumps(cls.datacache[date], cls=DecimalEncoder))

        return cls.datacache[date]

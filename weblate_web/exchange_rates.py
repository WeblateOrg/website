from __future__ import annotations

import datetime
import json
import logging
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import ClassVar

import requests

logger = logging.getLogger(__name__)
RATE_URL = "https://api.cnb.cz/cnbapi/exrates/daily"
BTC_RATE_URL = "https://api.exchange.coinbase.com/products/BTC-EUR/candles"
CACHE_DIR = Path.home() / ".cache" / "fakturace"
EXCHANGE_MARKUP = Decimal("1.1")
BTC_GRANULARITY = 86400


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
        response = requests.get(RATE_URL, params={"date": date}, timeout=10)
        response.raise_for_status()
        try:
            payload = json.loads(response.text, parse_float=Decimal)
        except json.JSONDecodeError as error:
            raise InvalidDataError(str(error)) from error
        return {item["currencyCode"]: item["rate"] for item in payload["rates"]}

    @classmethod
    def download_btc(cls, date: str) -> Decimal:
        rate_date = datetime.date.fromisoformat(date)
        next_date = rate_date + datetime.timedelta(days=1)
        response = requests.get(
            BTC_RATE_URL,
            params={
                "start": f"{date}T00:00:00Z",
                "end": f"{next_date.isoformat()}T00:00:00Z",
                "granularity": str(BTC_GRANULARITY),
            },
            timeout=10,
        )
        response.raise_for_status()
        try:
            payload = json.loads(response.text, parse_float=Decimal)
        except json.JSONDecodeError as error:
            raise InvalidDataError(str(error)) from error

        expected_timestamp = int(
            datetime.datetime.combine(
                rate_date, datetime.time.min, tzinfo=datetime.UTC
            ).timestamp()
        )
        if not isinstance(payload, list):
            raise InvalidDataError("BTC rate response is not a list")
        for candle in payload:
            if (
                isinstance(candle, list)
                and len(candle) >= 5
                and candle[0] == expected_timestamp
            ):
                # Coinbase candles contain time, low, high, open, close, volume.
                try:
                    btc_eur_rate = Decimal(candle[4])
                except (InvalidOperation, TypeError, ValueError) as error:
                    raise InvalidDataError("BTC closing rate is invalid") from error
                if btc_eur_rate <= 0:
                    raise InvalidDataError("BTC closing rate is not positive")
                return btc_eur_rate * cls.get("EUR", rate_date)
        raise InvalidDataError(f"BTC rate response has no candle for {date}")

    @classmethod
    def get(cls, currency: str, date: datetime.date, *, recursion: int = 0) -> Decimal:
        date_iso = date.isoformat()
        if currency == "CZK":
            return Decimal(1)
        try:
            if currency == "BTC":
                return cls.download_btc(date_iso)
            rates = cls.download(date_iso)
        except (requests.RequestException, InvalidDataError):
            logger.exception("failed to fetch exchange rate data")
            if recursion > 4:
                raise
            # Fallback on previous day if data is not available
            return cls.get(
                currency, date - datetime.timedelta(days=1), recursion=recursion + 1
            )
        return rates[currency]

    @classmethod
    def convert_from_eur(
        cls, amount: Decimal | float, currency: str, date: datetime.date
    ) -> Decimal:
        if not isinstance(amount, Decimal):
            amount = Decimal(amount)
        currency_rate = cls.get(currency, date)
        eur_rate = cls.get("EUR", date)
        return round(amount * EXCHANGE_MARKUP * eur_rate / currency_rate, 0)


class ExchangeRates(UncachedExchangeRates):
    datacache: ClassVar[dict[str, dict[str, Decimal]]] = {}
    btc_datacache: ClassVar[dict[str, Decimal]] = {}

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

    @classmethod
    def download_btc(cls, date: str) -> Decimal:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file = CACHE_DIR / f"rate-btc-{date}"

        if date not in cls.btc_datacache and cache_file.exists():
            cls.btc_datacache[date] = Decimal(json.loads(cache_file.read_text()))

        if date not in cls.btc_datacache:
            cls.btc_datacache[date] = super().download_btc(date)
            cache_file.write_text(
                json.dumps(cls.btc_datacache[date], cls=DecimalEncoder)
            )

        return cls.btc_datacache[date]

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, NotRequired, TypedDict

import sentry_sdk
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.utils.html import format_html
from django.utils.translation import gettext as _
from vies.types import VATIN
from zeep.exceptions import Error

if TYPE_CHECKING:
    from datetime import date

    from django_stubs_ext import StrOrPromise

VAT_VALIDITY_DAYS = 7


class VatinValidation(TypedDict):
    valid: bool
    fault_message: NotRequired[str]
    fault_code: NotRequired[str]
    countryCode: NotRequired[str]
    name: NotRequired[str]
    address: NotRequired[str]
    vatNumber: NotRequired[str]
    requestDate: NotRequired[date]


def cache_vies_data(
    value: str | VATIN, *, force: bool = False
) -> tuple[VATIN, VatinValidation]:
    result = value if isinstance(value, VATIN) else VATIN.from_str(value)
    key = f"VAT-{result}"
    data: VatinValidation | None = cache.get(key)
    if data is None or force:
        # Offline validation
        try:
            result.verify_country_code()
            result.verify_regex()
        except ValidationError:
            return result, {"valid": False}

        # Online validation
        try:
            vies_data = result.data
        except Error as error:
            data = {
                "valid": False,
                "fault_code": getattr(error, "code", "other:Error"),
                "fault_message": str(error),
            }
            sentry_sdk.capture_exception()
        else:
            data = {"valid": vies_data.valid}
            for field in VatinValidation.__annotations__:
                with suppress(AttributeError):
                    data[field] = getattr(vies_data, field)  # type: ignore[literal-required]
            cache.set(key, data, 3600 * 24 * VAT_VALIDITY_DAYS)

    return result, data


def validate_vatin_offline(value: str | VATIN) -> None:
    vatin = value if isinstance(value, VATIN) else VATIN.from_str(value)
    try:
        vatin.verify_country_code()
    except ValidationError as error:
        msg = _("{} is not a valid country code for any European Union member.")
        raise ValidationError(
            msg.format(vatin.country_code), code="Invalid country"
        ) from error
    try:
        vatin.verify_regex()
    except ValidationError as error:
        msg = _("{} does not match the country's VAT ID specifications.")
        raise ValidationError(msg.format(vatin), code="Invalid VAT syntax") from error


def validate_vatin(value: str | VATIN) -> None:
    vatin, vies_data = cache_vies_data(value)
    validate_vatin_offline(vatin)

    if not vies_data["valid"]:
        retry_errors = {"MS_UNAVAILABLE", "MS_MAX_CONCURRENT_REQ", "TIMEOUT"}
        retry_codes = {"soap:Server", "other:Error", "env:Server"}
        code = f"{vies_data.get('fault_code')}: {vies_data.get('fault_message')}"
        msg: StrOrPromise
        if (
            vies_data.get("fault_message") in retry_errors
            or vies_data.get("fault_code") in retry_codes
        ):
            msg = format_html(
                '{} <a href="{}" target="_blank">{}</a>',
                _(
                    "The official EU verification tool currently can't validate this VAT ID, please try again later."
                ),
                "https://ec.europa.eu/taxation_customs/vies/#/self-monitoring",
                _("View service status."),
            )
        else:
            msg = _("{} is not a valid VAT ID.").format(vatin)
        raise ValidationError(msg, code=code)

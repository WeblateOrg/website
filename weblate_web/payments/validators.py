from typing import NotRequired, TypedDict

import sentry_sdk
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.utils.html import format_html
from django.utils.translation import gettext as _
from vies.types import VATIN
from zeep.exceptions import Error


class VatinValidation(TypedDict):
    valid: bool
    fault_message: NotRequired[str]
    fault_code: NotRequired[str]


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
            data = {
                "valid": vies_data.valid,
                "fault_code": vies_data.get("fault_code", ""),
                "fault_message": vies_data.get("fault_message", ""),
            }
            cache.set(key, data, 3600 * 24 * 7)

    return result, data


def validate_vatin(value: str | VATIN) -> None:
    vatin, vies_data = cache_vies_data(value)
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

    if not vies_data["valid"]:
        retry_errors = {"MS_UNAVAILABLE", "MS_MAX_CONCURRENT_REQ", "TIMEOUT"}
        retry_codes = {"soap:Server", "other:Error", "env:Server"}
        code = "{}: {}".format(
            vies_data.get("fault_code"), vies_data.get("fault_message")
        )
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

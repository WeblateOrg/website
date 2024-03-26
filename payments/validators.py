import sentry_sdk
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.utils.html import format_html
from django.utils.translation import gettext as _
from vies.types import VATIN
from zeep.exceptions import Error


def cache_vies_data(value):
    if isinstance(value, str):
        value = VATIN.from_str(value)
    key = f"VAT-{value}"
    data = cache.get(key)
    if data is None:
        try:
            value.verify_country_code()
            value.verify_regex()
        except ValidationError:
            return value
        try:
            data = {}
            for item in value.data:
                data[item] = value.data[item]
            cache.set(key, data, 3600)
        except Error as error:
            data = {
                "valid": False,
                "fault_code": getattr(error, "code", "other:Error"),
                "fault_message": str(error),
            }
            sentry_sdk.capture_exception()
    value.__dict__["vies_data"] = data

    return value


def validate_vatin(value):
    value = cache_vies_data(value)
    try:
        value.verify_country_code()
    except ValidationError:
        msg = _("{} is not a valid country code for any European Union member.")
        raise ValidationError(msg.format(value.country_code))
    try:
        value.verify_regex()
    except ValidationError:
        msg = _("{} does not match the country's VAT ID specifications.")
        raise ValidationError(msg.format(value))

    if not value.vies_data["valid"]:
        retry_errors = {"MS_UNAVAILABLE", "MS_MAX_CONCURRENT_REQ", "TIMEOUT"}
        retry_codes = {"soap:Server", "other:Error", "env:Server"}
        if (
            value.vies_data.get("fault_message") in retry_errors
            or value.vies_data.get("fault_code") in retry_codes
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
            msg = _("{} is not a valid VAT ID.").format(value)
        raise ValidationError(msg)

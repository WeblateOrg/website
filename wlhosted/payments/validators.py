from __future__ import unicode_literals

import sentry_sdk
from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.utils.translation import ugettext as _
from suds import WebFault
from vies.types import VATIN


def cache_vies_data(value):
    if isinstance(value, str):
        value = VATIN.from_str(value)
    key = "VAT-{}".format(value)
    data = cache.get(key)
    if data is None:
        try:
            value.verify_country_code()
            value.verify_regex()
        except ValidationError:
            return value
        try:
            data = dict(value.data)
            cache.set(key, data, 3600)
        except WebFault as error:
            sentry_sdk.capture_exception()
            data = {"valid": False, "fault_reason": str(error.fault.faultstring)}
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
        msg = _("{} is not a valid VAT ID.")
        raise ValidationError(msg.format(value))

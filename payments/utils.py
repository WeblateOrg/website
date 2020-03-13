#
# Copyright © 2012 - 2020 Michal Čihař <michal@cihar.com>
#
# This file is part of Weblate <https://weblate.org/>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
#

"""
Helper functions and classees from Weblate.

The code here is copy of code from Weblate, taken from
weblate/utils/validators.py and weblate/utils/fields.py.
"""

import json
import re

from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.core.validators import validate_email as validate_email_django
from django.db import models
from django.utils.translation import gettext as _

# Reject some suspicious e-mail addresses, based on checks enforced by Exim MTA
EMAIL_BLACKLIST = re.compile(r"^([./|]|.*([@%!`#&?]|/\.\./))")


def validate_email(value):
    try:
        validate_email_django(value)
    except ValidationError:
        raise ValidationError(_("Enter a valid e-mail address."))
    user_part = value.rsplit("@", 1)[0]
    if EMAIL_BLACKLIST.match(user_part):
        raise ValidationError(_("Enter a valid e-mail address."))


class JSONField(models.TextField):
    """JSON serializaed TextField."""

    def __init__(self, **kwargs):
        if "default" not in kwargs:
            kwargs["default"] = {}
        super().__init__(**kwargs)

    def to_python(self, value):
        """Convert a string from the database to a Python value."""
        if not value:
            return None
        try:
            return json.loads(value)
        except ValueError:
            return value

    def get_prep_value(self, value):
        """Convert the value to a string that can be stored in the database."""
        if not value:
            return None
        if isinstance(value, (dict, list)):
            return json.dumps(value, cls=DjangoJSONEncoder)
        return super().get_prep_value(value)

    def from_db_value(self, value, *args, **kwargs):
        return self.to_python(value)

    def get_db_prep_save(self, value, *args, **kwargs):
        if value is None:
            value = {}
        return json.dumps(value, cls=DjangoJSONEncoder)

    def value_from_object(self, obj):
        value = super().value_from_object(obj)
        return json.dumps(value, cls=DjangoJSONEncoder)

#
# Copyright © 2012–2021 Michal Čihař <michal@cihar.com>
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
from django.template import Library
from django.utils import formats, timezone
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.utils.translation import pgettext

register = Library()


@register.filter
def recently(value):
    now = timezone.now()
    delta = now - value
    if delta.days > 12:
        return pgettext("123 translations ...", "this month")
    if delta.days >= 2:
        return pgettext("123 translations ...", "this week")
    if delta.days == 1:
        return pgettext("123 translations ...", "yesterday")
    if delta.seconds > 10000:
        return pgettext("123 translations ...", "today")
    if delta.seconds > 2000:
        return pgettext("123 translations ...", "recently")
    return pgettext("123 translations ...", "just now")


@register.filter
def days_diff_from_today(end):
    return (end - timezone.now()).days + 1


@register.filter
def date_format(value):
    return formats.date_format(value, pgettext("Date format", "d M Y"))


@register.simple_tag
def date_range(created, expires, bold: bool = False):
    created = escape(date_format(created))
    expires = escape(date_format(expires))
    if bold:
        expires = f"<strong>{expires}</strong>"

    return mark_safe(
        escape(pgettext("Date range", "%(created)s — %(expires)s"))
        % {
            "created": created,
            "expires": expires,
        }
    )

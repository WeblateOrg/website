#
# Copyright © Michal Čihař <michal@weblate.org>
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

from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib import messages
from django.http import HttpRequest
from django.urls import reverse
from django.utils.translation import gettext, override

if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from django.forms import Form

PAYMENTS_ORIGIN = "https://weblate.org/donate/process/"
FOSDEM_ORIGIN = "https://weblate.org/fosdem/"
AUTO_ORIGIN = "https://weblate.org/auto"


def get_site_url(name: str, *, strip_language: bool = True, **kwargs) -> str:
    if strip_language:
        with override("en"):
            url = reverse(name, kwargs=kwargs)
        url = url.removeprefix("/en")
    else:
        url = reverse(name, kwargs=kwargs)
    return f"{settings.SITE_URL}{url}"


class AuthenticatedHttpRequest(HttpRequest):
    user: User


def show_form_errors(request: HttpRequest, form: Form) -> None:
    """Show all form errors as a message."""
    for error in form.non_field_errors():
        messages.error(request, str(error))
    for field in form:
        for error in field.errors:
            messages.error(
                request,
                gettext("Error in parameter %(field)s: %(error)s")
                % {"field": field.name, "error": error},
            )

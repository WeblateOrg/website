#
# Copyright © 2012–2023 Michal Čihař <michal@cihar.com>
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

from datetime import datetime
from math import ceil

from django.conf import settings
from django.urls import reverse
from django.utils.functional import SimpleLazyObject
from django.utils.translation import override

from weblate_web.data import EXTENSIONS, VERSION
from weblate_web.models import Donation
from weblate_web.remote import get_activity, get_changes, get_contributors


def weblate_web(request):
    if request.resolver_match and request.resolver_match.url_name:
        match = request.resolver_match
        url_name = ":".join(match.namespaces + [match.url_name])
        url_kwargs = match.kwargs
    else:
        url_name = "home"
        url_kwargs = {}

    # Get canonical URl, unfortunately there seems to be no clean
    # way, so just strip /en/ from the URL
    # See also https://stackoverflow.com/a/27727877/225718
    with override("en"):
        canonical_url = reverse(url_name, kwargs=url_kwargs)
        if canonical_url.startswith("/en/"):
            canonical_url = canonical_url[3:]

    language_urls = []
    for code, name in settings.LANGUAGES:
        with override(code):
            language_urls.append(
                {
                    "name": name,
                    "code": code,
                    "url": reverse(url_name, kwargs=url_kwargs),
                }
            )

    downloads = [f"Weblate-{VERSION}.{ext}" for ext in EXTENSIONS]
    language_col = ceil(len(settings.LANGUAGES) / 3)

    return {
        "downloads": downloads,
        "canonical_url": canonical_url,
        "language_urls": language_urls,
        "donate_links": Donation.objects.filter(active=True, reward=3),
        "activity_sum": sum(get_activity()[-7:]),
        "contributors": SimpleLazyObject(get_contributors),
        "changes": SimpleLazyObject(get_changes),
        "current_year": datetime.utcnow().strftime("%Y"),
        "language_columns": [
            language_urls[:language_col],
            language_urls[language_col : language_col * 2],
            language_urls[language_col * 2 :],
        ],
    }

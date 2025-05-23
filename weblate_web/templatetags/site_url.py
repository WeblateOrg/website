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
"""Provide user friendly names for social authentication methods."""

from io import StringIO

from django import template
from django.utils.safestring import mark_safe
from lxml import etree

register = template.Library()


@register.filter
def add_site_url(content):
    """Automatically add site URL to any relative links or images."""
    parser = etree.HTMLParser()
    tree = etree.parse(StringIO(content), parser)
    for link in tree.findall(".//a"):
        url = link.get("href")
        if url is None:
            continue
        if url.startswith("/"):
            link.set("href", "https://weblate.org" + url)
    for link in tree.findall(".//img"):
        url = link.get("src")
        if url is None:
            continue
        if url.startswith("/"):
            link.set("src", "https://weblate.org" + url)
    return mark_safe(  # noqa: S308
        etree.tostring(
            tree.getroot(), pretty_print=True, method="html", encoding="unicode"
        )
    )

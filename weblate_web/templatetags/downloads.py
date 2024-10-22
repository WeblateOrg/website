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

from django.template import Library
from django.utils.translation import gettext as _
from django.utils.translation import ngettext

if TYPE_CHECKING:
    from weblate_web.remote import PYPIInfo

register = Library()


def filesizeformat(num_bytes: int) -> str:
    """
    Format the value like a 'human-readable' file size.

    For example 13 KB, 4.1 MB, 102 bytes, etc).
    """
    if num_bytes < 1024:
        return ngettext("%(size)d byte", "%(size)d bytes", num_bytes) % {
            "size": num_bytes
        }
    if num_bytes < 1024 * 1024:
        return _("%.1f KiB") % (num_bytes / 1024)
    if num_bytes < 1024 * 1024 * 1024:
        return _("%.1f MiB") % (num_bytes / (1024 * 1024))
    return _("%.1f GiB") % (num_bytes / (1024 * 1024 * 1024))


@register.inclusion_tag("snippets/download-link.html")
def downloadlink(info: PYPIInfo) -> dict[str, str]:
    name = info["filename"]

    if name.endswith(".tar.bz2"):
        text = _("Sources tarball, bzip2 compressed")
    elif name.endswith(".tar.gz"):
        text = _("Sources tarball, gzip compressed")
    elif name.endswith(".tar.xz"):
        text = _("Sources tarball, xz compressed")
    elif name.endswith(".zip"):
        text = _("Sources, zip compressed")
    elif name.endswith(".whl"):
        text = _("Python Wheel package")
    else:
        text = name

    size = filesizeformat(int(info["size"]))

    return {
        "url": info["url"],
        "name": name,
        "text": text,
        "size": size,
    }

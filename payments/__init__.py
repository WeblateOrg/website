#
# Copyright © 2012 - 2021 Michal Čihař <michal@cihar.com>
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

import vies.types


def monkey_patch_vies():
    """
    Ugly hack to remove GB from django_vies until proper solution is done.

    See https://github.com/codingjoe/django-vies/pull/168
    """
    if "GB" in vies.types.VIES_OPTIONS:
        del vies.types.VIES_OPTIONS["GB"]
        vies.types.VIES_COUNTRY_CHOICES = [
            item for item in vies.types.VIES_COUNTRY_CHOICES if item[0] != "GB"
        ]
        vies.types.MEMBER_COUNTRY_CODES = [
            item for item in vies.types.MEMBER_COUNTRY_CODES if item != "GB"
        ]


monkey_patch_vies()

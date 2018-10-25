# -*- coding: utf-8 -*-
#
# Copyright © 2012 - 2018 Michal Čihař <michal@cihar.com>
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

from __future__ import unicode_literals

from django.conf import settings
from django.urls import reverse

from django.utils.translation import get_language
from weblate.utils.site import get_site_url
from wlhosted.data import SUPPORTED_LANGUAGES


def get_origin():
    return get_site_url(reverse('create-billing'))


def get_payment_url(payment):
    language = get_language()
    if language not in SUPPORTED_LANGUAGES:
        language = 'en'
    return settings.PAYMENT_REDIRECT_URL.format(
        language=language,
        uuid=payment.uuid
    )

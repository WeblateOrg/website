# -*- coding: utf-8 -*-
#
# Copyright © 2012 - 2019 Michal Čihař <michal@cihar.com>
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
"""Remote data fetching and caching."""

from django.core.cache import cache
import requests
import sentry_sdk

CONTRIBUTORS_URL = 'https://api.github.com/repos/WeblateOrg/weblate/stats/contributors'
EXCLUDE_USERS = {'nijel', 'weblate'}


def get_contributors():
    key = 'wlweb-contributors'
    results = cache.get(key)
    if results is not None:
        return results
    # Perform request
    try:
        response = requests.get(CONTRIBUTORS_URL)
    except IOError as error:
        sentry_sdk.capture_exception(error)
        response = None
    # Stats are not yet calculated
    if response is None or response.status_code != 200:
        return []

    stats = response.json()
    # Fill in stats (these are chosen to be most representative)
    # as commits stats are misleading due to high number of commits generated
    # by old Weblate versions.
    for stat in stats:
        if stat['author']['login'] in EXCLUDE_USERS:
            stat['rank'] = 0
            continue
        stat['rank'] = 8 * stat['total'] + sum((week['a'] + week['d'] for week in stat['weeks']))

    stats.sort(key=lambda x: -x['rank'])

    cache.set(key, stats[:10], timeout=3600)
    return stats[:10]

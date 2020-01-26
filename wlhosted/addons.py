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
"""
Custom addons for Hosted Weblate
"""

from django.utils.translation import ugettext_lazy as _
from weblate.addons.events import EVENT_DAILY, EVENT_PRE_COMMIT
from weblate.addons.scripts import BaseAddon, BaseScriptAddon


class UnknownHorizonsTemplateAddon(BaseScriptAddon):
    # Event used to trigger the script
    events = (EVENT_PRE_COMMIT,)
    # Name of the addon, has to be unique
    name = "weblate.hosted.uh_scenario"
    # Verbose name and long descrption
    verbose = _("Generate Unknown Horizons scenario data")
    description = _("Generate Unknown Horizons scenario data")

    # Script to execute
    script = "/home/nijel/bin/uh_scenario_yaml"
    # File to add in commit (for pre commit event)
    # does not have to be set
    add_file = "content/scenarios/*_{{ language_code }}.yaml"

    @classmethod
    def can_install(cls, component, user):
        """Only useful for Unknown Horizons project"""
        if not component.project.slug == "uh":
            return False
        return super(UnknownHorizonsTemplateAddon, cls).can_install(component, user)


class ResetAddon(BaseAddon):
    # Event used to trigger the script
    events = (EVENT_DAILY,)
    # Name of the addon, has to be unique
    name = "weblate.hosted.reset"
    # Verbose name and long descrption
    verbose = _("Reset repository to upstream")
    description = _("Reset all changes in the local repository")
    repo_scope = True

    @classmethod
    def can_install(cls, component, user):
        # Only instalable on the sandbox project
        return component.project.slug == "sandbox"

    def daily(self, component):
        component.do_reset()

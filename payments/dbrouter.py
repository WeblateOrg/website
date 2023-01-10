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


class HostedRouter:
    """
    Database router for payments.

    A router to send payments app to separate database and
    block running migrations on that.
    """

    def db_for_read(self, model, **hints):
        """Redirect reads payments models go to payments_db."""
        if model._meta.app_label == "payments":
            return "payments_db"
        return None

    def db_for_write(self, model, **hints):
        """Redirect writes payments models go to payments_db."""
        if model._meta.app_label == "payments":
            return "payments_db"
        return None

    def allow_migrate(self, db, app_label, model_name=None, **hints):
        """Make sure the auth app only appears in the 'payments_db' database."""
        if app_label == "payments":
            return db == "payments_db"
        if db == "payments_db":
            return False
        return None

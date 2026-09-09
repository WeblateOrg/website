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

import logging
from contextlib import contextmanager
from typing import TYPE_CHECKING

from django.core.management.base import BaseCommand

from weblate_web.remote import fetch_vat_info

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def silence_vat_loggers() -> Iterator[None]:
    """Suppress noisy VIES and Zeep output for this background command."""
    loggers = [logging.getLogger(name) for name in ("vies", "zeep")]
    original_configuration = [
        (logger, logger.handlers, logger.propagate) for logger in loggers
    ]
    try:
        for logger in loggers:
            logger.handlers = [logging.NullHandler()]
            logger.propagate = False
        yield
    finally:
        for logger, handlers, propagate in original_configuration:
            logger.handlers = handlers
            logger.propagate = propagate


class Command(BaseCommand):
    help = "refreshes VAT caches"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--all",
            default=False,
            action="store_true",
            help="Fetch all VAT caches",
        )
        parser.add_argument(
            "--delay",
            default=30,
            type=int,
            help="Delay between API requests",
        )

    def handle(self, *args, **options) -> None:
        with silence_vat_loggers():
            fetch_vat_info(fetch_all=options["all"], delay=options["delay"])

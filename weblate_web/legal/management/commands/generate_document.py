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

from pathlib import Path
from typing import TYPE_CHECKING

from django.core.management.base import BaseCommand, CommandError
from django.template.loader import render_to_string
from ruamel.yaml import YAML

from weblate_web.invoices.models import BANK_ACCOUNTS
from weblate_web.pdf import render_pdf

if TYPE_CHECKING:
    from weblate_web.invoices.models import BankAccountInfo


class Command(BaseCommand):
    help = "generates legal document"
    client = None

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "document",
            type=Path,
            help="YAML description of the contract",
        )
        parser.add_argument(
            "params",
            type=Path,
            help="Contract parameters YAML",
        )
        parser.add_argument(
            "output",
            type=Path,
            help="Output PDF file",
        )

    def handle(self, document: Path, params: Path, output: Path, **kwargs) -> None:
        yaml = YAML()
        configuration = yaml.load(document)
        template = document.with_suffix(".html")

        context: dict[str, str | dict[str, BankAccountInfo]] = {
            "title": configuration["title"],
            "bank_accounts": {
                str(currency.label): bank for currency, bank in BANK_ACCOUNTS.items()
            },
        }

        for name, value in yaml.load(params).items():
            if name in context:
                raise CommandError(f"Duplicate parameter {name}")
            context[name] = value

        for name, info in configuration["params"].items():
            default: str | None = None
            choices: list[str] | None = None
            required: bool = True
            if info is not None:
                default = info.get("default")
                choices = info.get("choices")
                required = info.get("required", True)
            if name not in context:
                if default is not None:
                    context[name] = default
                elif required:
                    raise CommandError(f"Missing required parameter {name}")
            if choices is not None and context[name] not in choices:
                raise CommandError(f"{name} is not one of {choices!r}")

        render_pdf(html=render_to_string(template.as_posix(), context), output=output)

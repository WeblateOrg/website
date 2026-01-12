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

from shutil import copyfile
from typing import TYPE_CHECKING

from django.conf import settings
from django.db import models
from django.template.loader import render_to_string
from django.utils.translation import override

from weblate_web.const import (
    COMPANY_ADDRESS,
    COMPANY_CITY,
    COMPANY_COUNTRY,
    COMPANY_ID,
    COMPANY_NAME,
    COMPANY_VAT_ID,
    COMPANY_ZIP,
)
from weblate_web.pdf import render_pdf

if TYPE_CHECKING:
    from pathlib import Path


class AgreementKind(models.IntegerChoices):
    DPA = 1, "Data Processing Agreement"


class AgreementQuerySet(models.QuerySet):
    def order(self):
        return self.order_by("signed", "kind")


class Agreement(models.Model):
    customer = models.ForeignKey("payments.Customer", on_delete=models.deletion.PROTECT)
    signed = models.DateTimeField(auto_now_add=True)
    kind = models.IntegerField(choices=AgreementKind, default=AgreementKind.DPA)

    objects = AgreementQuerySet.as_manager()

    def __str__(self) -> str:
        return f"{self.kind_name} {self.customer.name} {self.shortdate}"

    def save(  # type: ignore[override]
        self,
        *,
        force_insert: bool = False,
        force_update: bool = False,
        using=None,
        update_fields=None,
    ) -> None:
        super().save(
            force_insert=force_insert,
            force_update=force_update,
            using=using,
            update_fields=update_fields,
        )
        self.generate_files()

    @property
    def shortdate(self) -> str:
        return self.signed.date().isoformat()

    @property
    def kind_name(self) -> str:
        return AgreementKind(self.kind).name

    @property
    def filename(self) -> str:
        """PDF filename."""
        return f"Weblate_{self.kind_name}_{self.customer.short_filename}_{self.shortdate}_{self.pk}.pdf"

    @property
    def path(self) -> Path:
        """PDF path object."""
        return settings.AGREEMENTS_PATH / self.filename

    def generate_files(self) -> None:
        self.generate_pdf()
        if settings.AGREEMENTS_COPY_PATH:
            copyfile(self.path, settings.AGREEMENTS_COPY_PATH / self.filename)

    def generate_pdf(self) -> None:
        # Create directory to store agreements
        settings.AGREEMENTS_PATH.mkdir(exist_ok=True)
        render_pdf(
            html=self.render_html(),
            output=settings.AGREEMENTS_PATH / self.filename,
        )

    def render_html(self) -> str:
        with override("en_GB"):
            return render_to_string(
                "pdf/dpa.html",
                {
                    "customer": self.customer,
                    "signed": self.signed,
                    "title": self.get_kind_display(),
                    "company_name": COMPANY_NAME,
                    "company_address": COMPANY_ADDRESS,
                    "company_zip": COMPANY_ZIP,
                    "company_city": COMPANY_CITY,
                    "company_country": COMPANY_COUNTRY,
                    "company_vat_id": COMPANY_VAT_ID,
                    "company_id": COMPANY_ID,
                },
            )

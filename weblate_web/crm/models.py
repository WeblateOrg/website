from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.storage import FileSystemStorage
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from weblate_web.payments.models import Customer

if TYPE_CHECKING:
    from collections.abc import Callable

CRM_STORAGE = FileSystemStorage(location=settings.CRM_ROOT)


@dataclass(frozen=True)
class InteractionDetailRow:
    label: object
    value: object
    url: str = ""


class InteractionQuerySet(models.QuerySet["Interaction", "Interaction"]):
    def order(self) -> InteractionQuerySet:
        return self.order_by("-timestamp", "origin", "summary", "pk")


class Interaction(models.Model):
    class Origin(models.IntegerChoices):
        EMAIL = 1, "Outbound e-mail"
        MERGE = 2, "Merged customer"
        ZAMMAD_ATTACHMENT = 3, "Attachment exchanged in Zammad"
        VIES = 4, "VIES validation"
        MANUAL_PAYMENT = 5, "Manual payment"
        MAINTENANCE_WINDOW = 6, "Maintenance window"
        MANUAL_NOTE = 7, "Manual note"

    timestamp = models.DateTimeField(default=timezone.now, verbose_name="Timestamp")
    origin = models.IntegerField(choices=Origin, verbose_name="Origin")
    customer = models.ForeignKey(Customer, on_delete=models.RESTRICT)
    summary = models.CharField(max_length=200, verbose_name="Summary")
    content = models.TextField(verbose_name="Content")
    details = models.JSONField(default=dict, blank=True, encoder=DjangoJSONEncoder)
    attachment = models.FileField(
        storage=CRM_STORAGE, upload_to="attachments", verbose_name="Attachment"
    )
    user = models.ForeignKey(User, null=True, on_delete=models.RESTRICT)
    remote_id = models.IntegerField(
        verbose_name="Remote ID",
        help_text="For example Zammad attachment ID",
        default=0,
    )

    objects = InteractionQuerySet.as_manager()

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.timestamp.isoformat()} [{self.customer}:{self.get_origin_display()}]: {self.summary}"

    @staticmethod
    def _format_detail_value(value: Any) -> object:
        if isinstance(value, bool):
            return _("Yes") if value else _("No")
        if isinstance(value, list | tuple | set):
            return ", ".join(str(item) for item in value)
        return value

    @classmethod
    def _add_row(
        cls,
        rows: list[InteractionDetailRow],
        label: object,
        value: Any,
        url: str = "",
        *,
        include_empty: bool = False,
    ) -> None:
        empty_string = isinstance(value, str) and not value
        empty_collection = isinstance(value, list | tuple | set) and not value
        if value is None or empty_collection or (empty_string and not include_empty):
            return
        if empty_string and include_empty:
            value = _("not set")
        rows.append(InteractionDetailRow(label, cls._format_detail_value(value), url))

    @staticmethod
    def _make_url(viewname: str, **kwargs: object) -> str:
        return reverse(viewname, kwargs=kwargs)

    @property
    def content_is_html(self) -> bool:
        return self.origin == self.Origin.EMAIL

    @property
    def primary_content(self) -> str:
        if self.origin == self.Origin.EMAIL:
            return str(self.details.get("subject") or self.summary)
        if self.content:
            return self.content.splitlines()[0]
        return self.summary

    @property
    def attachment_filename(self) -> str:
        if not self.attachment:
            return ""
        return os.path.basename(self.attachment.name)

    @property
    def attachment_download_url(self) -> str:
        if not self.attachment:
            return ""
        return self._make_url("crm:interaction-download", pk=self.pk)

    @property
    def has_detail_page(self) -> bool:
        return bool(self.content or self.details)

    @property
    def detail_rows(self) -> list[InteractionDetailRow]:
        renderers: dict[int, Callable[[], list[InteractionDetailRow]]] = {
            int(self.Origin.EMAIL): self._email_detail_rows,
            int(self.Origin.MERGE): self._merge_detail_rows,
            int(self.Origin.ZAMMAD_ATTACHMENT): self._zammad_attachment_detail_rows,
            int(self.Origin.VIES): self._vies_detail_rows,
            int(self.Origin.MANUAL_PAYMENT): self._manual_payment_detail_rows,
            int(self.Origin.MAINTENANCE_WINDOW): self._maintenance_window_detail_rows,
        }
        rows = renderers.get(self.origin, self._generic_detail_rows)()
        return rows or self._generic_detail_rows()

    def _email_detail_rows(self) -> list[InteractionDetailRow]:
        rows: list[InteractionDetailRow] = []
        self._add_row(rows, _("Notification"), self.details.get("notification"))
        self._add_row(rows, _("Recipients"), self.details.get("recipients"))
        self._add_row(rows, _("Invoice"), self.details.get("invoice"))
        self._add_row(rows, _("Attachment"), self.details.get("attachment"))
        return rows

    def _merge_detail_rows(self) -> list[InteractionDetailRow]:
        rows: list[InteractionDetailRow] = []
        self._add_row(
            rows,
            _("Merged customer"),
            self.details.get("merged_customer") or self.content,
        )
        self._add_row(
            rows, _("Merged customer ID"), self.details.get("merged_customer_id")
        )
        self._add_row(
            rows,
            _("Merged customer email"),
            self.details.get("merged_customer_email"),
        )
        self._add_row(
            rows,
            _("Merged customer end client"),
            self.details.get("merged_customer_end_client"),
        )
        return rows

    def _zammad_attachment_detail_rows(self) -> list[InteractionDetailRow]:
        rows: list[InteractionDetailRow] = []
        self._add_row(
            rows,
            _("Filename"),
            self.details.get("filename") or self.content or self.summary,
        )
        self._add_row(rows, _("Ticket ID"), self.details.get("ticket_id"))
        self._add_row(rows, _("Article ID"), self.details.get("article_id"))
        self._add_row(
            rows,
            _("Attachment ID"),
            self.details.get("attachment_id") or self.remote_id,
        )
        return rows

    def _vies_detail_rows(self) -> list[InteractionDetailRow]:
        rows: list[InteractionDetailRow] = []
        self._add_row(rows, _("Automated"), self.details.get("automated"))
        self._add_row(rows, _("Error code"), self.details.get("code"))
        self._add_row(rows, _("Error message"), self.details.get("message"))
        return rows

    def _manual_payment_detail_rows(self) -> list[InteractionDetailRow]:
        rows: list[InteractionDetailRow] = []
        payment_id = self.details.get("payment_id")
        self._add_row(rows, _("Invoice"), self.details.get("invoice"))
        self._add_row(rows, _("Amount"), self.details.get("amount"))
        self._add_row(
            rows,
            _("Payment"),
            payment_id,
            self._make_url("admin:payments_payment_change", object_id=payment_id)
            if payment_id
            else "",
        )
        self._add_row(rows, _("Confirmed by"), self.details.get("confirmed_by"))
        self._add_row(rows, _("Confirmed at"), self.details.get("confirmed_at"))
        self._add_row(rows, _("Description"), self.details.get("description"))
        return rows

    def _maintenance_window_detail_rows(self) -> list[InteractionDetailRow]:
        rows: list[InteractionDetailRow] = []
        details = dict(self.details)
        service_id = details.get("service_id")
        self._add_row(
            rows,
            _("Service"),
            details.get("service_title") or service_id,
            self._make_url("crm:service-detail", pk=service_id) if service_id else "",
        )
        if "old_value" in details:
            self._add_row(
                rows,
                _("Previous maintenance window"),
                details["old_value"],
                include_empty=True,
            )
        self._add_row(
            rows,
            _("Maintenance window"),
            details.get("new_value") if "new_value" in details else self.content,
            include_empty=True,
        )
        self._add_row(rows, _("Service URL"), details.get("service_url"))
        return rows

    def _generic_detail_rows(self) -> list[InteractionDetailRow]:
        rows: list[InteractionDetailRow] = []
        for key, value in sorted(self.details.items()):
            self._add_row(rows, key.replace("_", " ").capitalize(), value)
        return rows


class ZammadSyncLog(models.Model):
    timestamp = models.DateTimeField(auto_now_add=True, verbose_name="Timestamp")
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE)
    article_id = models.IntegerField(unique=True)

    def __str__(self):
        return f"{self.customer}: {self.article_id}"

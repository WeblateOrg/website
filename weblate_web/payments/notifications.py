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

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import UUID

import sentry_sdk
from django.conf import settings
from django.db import DatabaseError, transaction
from django.http import Http404, HttpResponse, HttpResponseBadRequest
from django.utils import timezone
from django.utils.translation import override
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from .backends import ThePay2Card, get_backend
from .models import Payment, ThePayNotification

if TYPE_CHECKING:
    from django.http import HttpRequest


def _thepay_backend(name: str) -> type[ThePay2Card] | None:
    try:
        backend = get_backend(name)
    except KeyError:
        return None
    return backend if issubclass(backend, ThePay2Card) else None


def is_thepay_payment(pk: UUID) -> bool:
    backend = Payment.objects.filter(pk=pk).values_list("backend", flat=True).first()
    return backend is not None and _thepay_backend(backend) is not None


@never_cache
def enqueue_thepay_notification(
    request: HttpRequest, pk: UUID, *, legacy_language: str = ""
) -> HttpResponse:
    for field in ("project_id", "payment_uid", "type"):
        if len(request.GET.getlist(field)) != 1:
            return HttpResponseBadRequest()

    try:
        payment_uid = UUID(request.GET["payment_uid"])
    except ValueError:
        return HttpResponseBadRequest()

    project_id = getattr(settings, "THEPAY_PROJECT_ID", None)
    if (
        project_id is None
        or request.GET["project_id"] != str(project_id)
        or payment_uid != pk
        or request.GET["type"] not in {"state_changed", "offset_account_obtained"}
    ):
        return HttpResponseBadRequest()

    try:
        with transaction.atomic(durable=True):
            ThePayNotification.enqueue(pk, legacy_language=legacy_language)
    except DatabaseError as error:
        sentry_sdk.capture_exception(error)
        return HttpResponse(status=503)
    return HttpResponse()


@require_GET
def thepay_notification(request: HttpRequest, pk: UUID) -> HttpResponse:
    if not is_thepay_payment(pk):
        raise Http404
    return enqueue_thepay_notification(request, pk)


@transaction.atomic
def _process_thepay_notification(notification: ThePayNotification) -> None:
    work = ThePayNotification.objects.filter(
        pk=notification.pk, revision=notification.revision
    )
    payment = Payment.objects.filter(pk=notification.pk).first()
    if payment is None or (backend_class := _thepay_backend(payment.backend)) is None:
        work.delete()
        return

    backend = backend_class(payment)
    payment = backend.payment
    # A concurrent worker may have handled or rescheduled this revision while
    # we waited for the payment lock.
    if not work.filter(ready_at__lte=timezone.now()).exists():
        return

    if payment.backend == backend.name and payment.state in {
        Payment.PENDING,
        Payment.REJECTED,
        Payment.CANCELLED,
    }:
        language = (
            payment.details.get("thepay_language")
            or notification.legacy_language
            or settings.LANGUAGE_CODE
        )
        with override(language):
            backend.complete_notification()
        if backend.awaiting_checkout_verification:
            work.update(ready_at=timezone.now() + timedelta(minutes=1))
            return

    work.delete()


def process_thepay_notifications() -> None:
    work = ThePayNotification.objects.filter(ready_at__lte=timezone.now()).order_by(
        "ready_at", "pk"
    )
    for notification in list(work):
        try:
            _process_thepay_notification(notification)
        except Exception as error:  # pylint: disable=broad-exception-caught
            # Defer failed settlement after rollback so the rest of the batch can run.
            sentry_sdk.capture_exception(error)
            ThePayNotification.objects.filter(
                pk=notification.pk, revision=notification.revision
            ).update(ready_at=timezone.now() + timedelta(minutes=1))

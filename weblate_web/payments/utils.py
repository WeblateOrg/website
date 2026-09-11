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

"""
Helper functions and classes from Weblate.

The code here is copy of code from Weblate, taken from
weblate/utils/validators.py and weblate/utils/fields.py.
"""

from __future__ import annotations

import re
from email.mime.image import MIMEImage
from email.utils import make_msgid
from pathlib import Path
from typing import TYPE_CHECKING

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.core.mail.message import SafeMIMEMultipart
from django.core.validators import validate_email as validate_email_django
from django.template.loader import render_to_string
from django.utils.translation import get_language, get_language_bidi
from django.utils.translation import gettext as _
from html2text import HTML2Text

from weblate_web.const import COMPANY_BILLING_EMAIL

if TYPE_CHECKING:
    from collections.abc import Sequence
    from email.message import Message

    from weblate_web.invoices.models import Invoice

# Reject some suspicious e-mail addresses, based on checks enforced by Exim MTA
EMAIL_BLACKLIST = re.compile(r"^([./|]|.*([@%!`#&?]|/\.\./))")


def validate_email(value) -> None:
    try:
        validate_email_django(value)
    except ValidationError as error:
        raise ValidationError(_("Enter a valid e-mail address.")) from error
    user_part = value.rsplit("@", 1)[0]
    if EMAIL_BLACKLIST.match(user_part):
        raise ValidationError(_("Enter a valid e-mail address."))


class NotificationEmail(EmailMultiAlternatives):
    """Keep branding images within the HTML alternative."""

    inline_images: tuple[tuple[str, bytes, str], ...] = ()

    def _create_alternatives(self, msg: Message) -> Message:
        # Django's stubs omit this MIME construction hook.
        message = super()._create_alternatives(msg)  # type: ignore[misc]
        if not self.inline_images or not message.is_multipart():
            return message
        parts = []
        for part in message.get_payload():
            if part.get_content_type() == "text/html":
                related = SafeMIMEMultipart(
                    _subtype="related",
                    encoding=self.encoding or settings.DEFAULT_CHARSET,
                    type="text/html",
                )
                related.attach(part)
                for name, content, cid in self.inline_images:
                    image = MIMEImage(content, _subtype="png")
                    image.add_header("Content-ID", cid)
                    image.add_header("Content-Disposition", "inline", filename=name)
                    related.attach(image)
                parts.append(related)
            else:
                parts.append(part)
        message.set_payload(parts)
        return message


def send_notification(
    notification: str,
    recipients: Sequence[str],
    invoice: Invoice | None = None,
    **kwargs,
) -> EmailMultiAlternatives:
    # HTML to text conversion
    html2text = HTML2Text(bodywidth=78)
    html2text.unicode_snob = True
    html2text.ignore_images = True
    html2text.pad_tables = True

    # Logos
    images = tuple(
        (
            name,
            (Path(settings.STATIC_ROOT) / name).read_bytes(),
            make_msgid(domain="cid.weblate.org"),
        )
        for name in ("email-logo.png", "email-logo-footer.png")
    )

    # Context and subject
    context = {
        "LANGUAGE_CODE": get_language(),
        "LANGUAGE_BIDI": get_language_bidi(),
    }
    context.update(kwargs)
    subject = render_to_string(f"mail/{notification}_subject.txt", context).strip()
    context["subject"] = subject

    # Render body
    body = render_to_string(f"mail/{notification}.html", context).strip()

    # Prepare e-mail
    email = NotificationEmail(
        subject,
        html2text.handle(body),
        COMPANY_BILLING_EMAIL,
        recipients,
    )
    email.inline_images = images
    for name, _content, cid in images:
        body = body.replace(f"cid:{name}@cid.weblate.org", f"cid:{cid[1:-1]}")
    email.attach_alternative(body, "text/html")
    # Include invoice PDF if exists
    if invoice is not None and not invoice.is_draft:
        email.attach(invoice.filename, invoice.path.read_bytes(), "application/pdf")
        if invoice.is_paid:
            email.attach(
                invoice.receipt_filename,
                invoice.receipt_path.read_bytes(),
                "application/pdf",
            )
    if recipients:
        email.send()
    return email

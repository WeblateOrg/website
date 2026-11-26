# Copyright © Michal Čihař <michal@weblate.org>
# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from string import ascii_lowercase, ascii_uppercase

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from idna import IDNAError, encode

ASCII_LOWER = str.maketrans(ascii_uppercase, ascii_lowercase)
MAX_ADDRESS_LENGTH = 254


def mailbox_address(email: str) -> str | None:
    """Return the SMTP address and comparison key for an invitation mailbox."""
    if len(email) > MAX_ADDRESS_LENGTH:
        return None
    local, separator, domain = email.rpartition("@")
    if not separator or not local.isascii():
        return None
    domain = domain.translate(ASCII_LOWER)
    try:
        validate_email(email)
        if not domain.startswith("["):
            # Django's SMTP backend applies legacy IDNA to Unicode recipients.
            # Give it an ASCII address produced by strict IDNA2008 instead.
            domain = encode(domain, strict=True).decode("ascii")
    except (IDNAError, ValidationError):
        return None
    address = f"{local}@{domain}"
    return address if len(address) <= MAX_ADDRESS_LENGTH else None

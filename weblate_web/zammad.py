from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from django.conf import settings
from zammad_py import ZammadAPI

from weblate_web.utils import get_site_url

if TYPE_CHECKING:
    from weblate_web.crm.models import Interaction
    from weblate_web.models import Subscription


def get_zammad_client() -> ZammadAPI:
    return ZammadAPI(
        url="https://care.weblate.org/api/v1/",
        http_token=settings.ZAMMAD_TOKEN,
    )


def create_dedicated_hosting_ticket(subscription: Subscription) -> None:
    zammad = get_zammad_client()

    # Get first user email (there should be only one for initial subscription anyway)
    emails: list[str] = subscription.service.user_emails.split(",")
    email: str = emails[0]
    if not email:
        raise ValueError(f"Subscription without an email: {subscription}")

    # Extract TLD as best guess for cloud name
    domain: str = email.split("@")[1].split(".", maxsplit=1)[0]

    zammad.ticket.create(
        params={
            "title": f"Your dedicated Weblate instance ({domain})",
            "customer_id": f"guess:{email}",
            "group": "Users",
            "tags": "dedicated",
            "article": {
                "subject": "Your dedicated Weblate instance",
                "from": "Weblate Care",
                "to": email,
                "cc": ",".join(emails[1:]),
                "body": f"""Hello,

Thank you for purchasing a dedicated Weblate instance! We will prepare it promptly after you provide the information below.

If you want to use your own domain, please create a CNAME DNS entry to {domain}.weblate.cloud and let us know.
Your weblate.cloud domain name can also be changed; if the one mentioned above doesn't fit you, tell us.

In case you would like to use another authentication method(s) than the native username+password login, please provide us credentials for your chosen method.
You can check available options like GitHub, OAuth, SAML, Azure AD, and many more at https://docs.weblate.org/en/latest/admin/auth.html#social-authentication.

It is also possible to keep settings default and customize anything later. It’s your instance, your choice. We will also gladly help with any of your Weblate questions; please don’t hesitate to ask by replying to this message.

Kind regards from Weblate
""",
                "type": "email",
                "sender": "Agent",
                "internal": False,
            },
        }
    )


def create_invoice_request_ticket(interaction: Interaction) -> dict[str, Any]:
    client = get_zammad_client()
    details = interaction.details
    crm_url = get_site_url("crm:service-detail", pk=details["service_id"])
    query = urlencode(
        {
            "subscription": details["subscription_id"],
            "customer_reference": details["customer_reference"],
        }
    )
    return _create_invoice_request_resource(
        client,
        "tickets",
        {
            "title": f"Renewal invoice request: {details['package']} ({details['subscription_id']})",
            "customer_id": f"guess:{details['requester_email']}",
            "group": "Users",
            "article": {
                "subject": "Invoice request processing",
                "body": f"CRM: {crm_url}?{query}#subscription-{details['subscription_id']}",
                "content_type": "text/plain",
                "type": "note",
                "sender": "Agent",
                "internal": True,
            },
        },
    )


def create_invoice_request_article(interaction: Interaction) -> None:
    client = get_zammad_client()
    _create_invoice_request_resource(
        client,
        "ticket_articles",
        {
            "ticket_id": interaction.remote_id,
            "subject": "Renewal invoice request",
            "body": interaction.content,
            "from": interaction.details["requester_email"],
            "content_type": "text/plain",
            "type": "web",
            "sender": "Customer",
            "internal": False,
        },
    )


def _create_invoice_request_resource(
    client: ZammadAPI, resource: str, params: dict[str, Any]
) -> dict[str, Any]:
    # The library discards HTTPError.response; preserve it to distinguish rejections
    # from ambiguous failures, and bound the request duration.
    response = client.session.post(f"{client.url}{resource}", json=params, timeout=30)
    response.raise_for_status()
    return response.json()

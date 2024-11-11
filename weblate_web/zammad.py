from __future__ import annotations

from typing import TYPE_CHECKING

from django.conf import settings
from zammad_py import ZammadAPI

if TYPE_CHECKING:
    from weblate_web.models import Subscription


def get_zammad_client() -> ZammadAPI:
    return ZammadAPI(
        url="https://care.weblate.org/api/v1/",
        http_token=settings.ZAMMAD_TOKEN,
    )


def create_dedicated_hosting_ticket(subscription: Subscription) -> None:
    zammad = get_zammad_client()

    # Get first user email (there should be only one for initial subscription anyway)
    email = subscription.service.user_emails.split(",")[0]

    # Extract TLD as best guess for cloud name
    domain = email.split("@")[1].split(".")[0]

    zammad.ticket.create(
        params={
            "title": "Your dedicated Weblate instance",
            "customer_id": f"guess:{email}",
            "group": "Users",
            "article": {
                "subject": "Your dedicated Weblate instance",
                "from": "Weblate Care",
                "to": email,
                "body": f"""Hello,

Thank you for purchasing a dedicated Weblate instance! We will prepare it promptly after you provide the information below.

If you want to use your own domain, please create a CNAME DNS entry to {domain}.weblate.cloud and let us know.
The Weblate domain name can also be changed if the mentioned one does not fit you.

In case you would like to use another authentication method(s) than native login, please provide us credentials for chosen method.
You can check the options at https://docs.weblate.org/en/latest/admin/auth.html#authentication.

It is also possible to keep settings default and customize anything later. We will also happily help with any of your Weblate questions.

--
Weblate
""",
                "type": "email",
                "sender": "Agent",
                "internal": False,
            },
        }
    )

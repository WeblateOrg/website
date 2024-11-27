from __future__ import annotations

import re
from typing import TYPE_CHECKING

import requests
from django.conf import settings
from paramiko.client import SSHClient

if TYPE_CHECKING:
    from weblate_web.payments.models import Customer

    from .models import Report, Service

SUBACCOUNTS_API = "https://robot-ws.your-server.de/storagebox/{}/subaccount"


def create_storage_folder(
    dirname: str, service: Service, customer: Customer, last_report: Report
):
    # Create folder and SSH key
    client = SSHClient()
    client.load_system_host_keys()
    client.connect(
        hostname=settings.STORAGE_SSH_HOSTNAME,
        port=settings.STORAGE_SSH_PORT,
        username=settings.STORAGE_SSH_USER,
    )
    ftp = client.open_sftp()
    ftp.mkdir(dirname)
    ftp.chdir(dirname)
    with ftp.open("README.txt", "w") as handle:
        handle.write(f"""Weblate Cloud Backup
====================

Service: {service.pk}
Customer: {customer.name}
""")

    ftp.mkdir(".ssh")
    ftp.chdir(".ssh")
    with ftp.open("authorized_keys", "w") as handle:
        handle.write(last_report.ssh_key)


def generate_subaccount_data(
    dirname: str, service: Service, customer: Customer
) -> dict[str, str]:
    # Remove not allowed characters
    customer_name = re.sub(r"[^A-Za-z0-9,.]+", " ", customer)
    return {
        "homedirectory": f"weblate/{dirname}",
        "ssh": "1",
        "external_reachability": "1",
        "comment": f"Weblate backup ({service.pk}) {customer_name}"[:50],
    }


def create_storage_subaccount(
    dirname: str, service: Service, customer: Customer
) -> dict:
    # Create account on the service
    url = SUBACCOUNTS_API.format(settings.STORAGE_BOX)
    response = requests.post(
        url,
        data=generate_subaccount_data(dirname, service, customer),
        auth=(settings.STORAGE_USER, settings.STORAGE_PASSWORD),
        timeout=720,
    )
    response.raise_for_status()
    return response.json()


def modify_storage_subaccount(username: str, data: dict) -> None:
    # Create account on the service
    url = f"{SUBACCOUNTS_API.format(settings.STORAGE_BOX)}/{username}"
    response = requests.put(
        url,
        data=data,
        auth=(settings.STORAGE_USER, settings.STORAGE_PASSWORD),
        timeout=720,
    )
    response.raise_for_status()


def generate_ssh_url(data: dict) -> str:
    return "ssh://{}@{}:23/./backups".format(
        data["subaccount"]["username"], data["subaccount"]["server"]
    )


def get_storage_subaccounts() -> list[dict]:
    url = SUBACCOUNTS_API.format(settings.STORAGE_BOX)
    response = requests.get(
        url, auth=(settings.STORAGE_USER, settings.STORAGE_PASSWORD), timeout=720
    )
    response.raise_for_status()
    return response.json()

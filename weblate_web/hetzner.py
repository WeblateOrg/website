from __future__ import annotations

import stat
from contextlib import contextmanager
from typing import TYPE_CHECKING

import requests
from django.conf import settings
from paramiko.client import SSHClient

if TYPE_CHECKING:
    from collections.abc import Generator

    from paramiko.sftp_attr import SFTPAttributes
    from paramiko.sftp_client import SFTPClient

    from weblate_web.payments.models import Customer

    from .models import Report, Service

SUBACCOUNTS_API = "https://robot-ws.your-server.de/storagebox/{}/subaccount"


@contextmanager
def sftp_client() -> Generator[SFTPClient]:
    with SSHClient() as client:
        client.load_system_host_keys()
        client.connect(
            hostname=settings.STORAGE_SSH_HOSTNAME,
            port=settings.STORAGE_SSH_PORT,
            username=settings.STORAGE_SSH_USER,
        )
        yield client.open_sftp()


def get_directory_summary(
    ftp: SFTPClient, dirname: str, dirstat: SFTPAttributes | None = None
) -> tuple[int, int]:
    if dirstat is None:
        dirstat = ftp.stat(dirname)
    size = 0
    mtime = dirstat.st_mtime or 0
    for attr in ftp.listdir_attr(dirname):
        if attr.st_mode is None or attr.st_size is None or attr.st_mtime is None:
            raise ValueError(f"Incomplete attributes {attr}")
        if stat.S_ISDIR(attr.st_mode):
            ftp.chdir(dirname)
            dir_size, dir_mtime = get_directory_summary(
                ftp, attr.filename, dirstat=attr
            )
            ftp.chdir("..")
            size += dir_size
            mtime = max(mtime, dir_mtime)
        else:
            size += attr.st_size
            mtime = max(mtime, attr.st_mtime)
    return size, mtime


def create_storage_folder(
    dirname: str, service: Service, customer: Customer, last_report: Report
) -> None:
    # Create folder and SSH key
    with sftp_client() as ftp:
        ftp.mkdir(dirname)
        ftp.chdir(dirname)
        with ftp.open("README.txt", "w") as handle:
            handle.write(f"""Weblate Cloud Backup
    ====================

    Service: {service.pk}
    Site: {service.site_domain}
    Customer: {customer.name}
    """)

        ftp.mkdir(".ssh")
        ftp.chdir(".ssh")
        with ftp.open("authorized_keys", "w") as handle:
            handle.write(last_report.ssh_key)


def generate_subaccount_data(
    dirname: str, service: Service, *, access: bool = True
) -> dict[str, str]:
    return {
        "homedirectory": f"weblate/{dirname}",
        "ssh": "1" if access else "0",
        "external_reachability": "1",
        "comment": f"Weblate backup ({service.pk}) {service.site_domain}"[:50],
    }


def extract_field(data: dict, field: str) -> str:
    """
    Hides boolean API weirdness when parsing responses.

    - booleans need to be written as strings
    - but they are returned as booleans
    """
    value = data[field]
    if field in {"ssh", "external_reachability"}:
        return str(int(value))
    return value


def create_storage_subaccount(dirname: str, service: Service) -> dict:
    # Create account on the service
    url = SUBACCOUNTS_API.format(settings.STORAGE_BOX)
    response = requests.post(
        url,
        data=generate_subaccount_data(dirname, service),
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

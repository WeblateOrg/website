from __future__ import annotations

import stat
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING, Literal, NotRequired, TypedDict, cast

import requests
from django.conf import settings
from django.utils.crypto import get_random_string
from paramiko.client import SSHClient

if TYPE_CHECKING:
    from collections.abc import Generator

    from paramiko.sftp_attr import SFTPAttributes
    from paramiko.sftp_client import SFTPClient

    from weblate_web.payments.models import Customer

    from .models import Report, Service

STORAGE_BOX_API = "https://api.hetzner.com/v1/storage_boxes/{}"


class ErrorDict(TypedDict):
    code: str
    message: str


class ResourceDict(TypedDict):
    id: int
    type: str


class ActionDict(TypedDict):
    id: int
    command: str
    status: Literal["running", "success", "error"]
    started: str
    finished: str | None
    progress: int
    resources: list[ResourceDict]
    error: ErrorDict | None


class AccessSettingsDict(TypedDict):
    samba_enabled: bool
    ssh_enabled: bool
    webdav_enabled: bool
    readonly: bool
    reachable_externally: bool


class SubaccountInfoDict(TypedDict):
    home_directory: str
    labels: dict[str, str]
    description: str
    access_settings: AccessSettingsDict
    password: NotRequired[str]


class SubaccountDict(SubaccountInfoDict):
    id: int
    username: str
    server: str
    created: str
    storage_box: int


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
) -> SubaccountInfoDict:
    return {
        "home_directory": f"weblate/{dirname}",
        "access_settings": {
            "samba_enabled": False,
            "ssh_enabled": access,
            "webdav_enabled": False,
            "readonly": False,
            "reachable_externally": True,
        },
        "labels": {
            "environment": service.site_domain,
        },
        "description": f"Weblate backup ({service.pk}) {service.site_domain}"[:50],
    }


def get_hetzner_headers() -> dict[str, str]:
    """Hetzner API authorization headers."""
    return {"Authorization": f"Bearer {settings.HETZNER_API}"}


def hetzner_box_url(*parts: str) -> str:
    """URL for Hetzner Storage Box API."""
    base_url = STORAGE_BOX_API.format(settings.STORAGE_BOX)
    return "/".join((base_url, *parts))


def wait_for_action(action: ActionDict) -> ActionDict:
    """Wait for action to complete."""
    action_url = hetzner_box_url("actions", str(action["id"]))

    # Wait until action is completed
    while action["error"] is None and action["status"] == "running":
        time.sleep(4)
        response = requests.get(
            action_url,
            headers=get_hetzner_headers(),
            timeout=60,
        )
        response.raise_for_status()
        action = response.json()["action"]

    # Error handling
    if action["error"] is not None:
        raise ValueError(f"Creating subaccount failed: {action['error']['message']}")

    return action


def create_storage_subaccount(dirname: str, service: Service) -> SubaccountDict:
    """Create account on the service."""
    url = hetzner_box_url("subaccounts")
    data = generate_subaccount_data(dirname, service)
    data["password"] = get_random_string(32)
    # Create subaccount
    response = requests.post(
        url,
        json=data,
        headers=get_hetzner_headers(),
        timeout=60,
    )
    response.raise_for_status()
    action = wait_for_action(response.json()["action"])

    # Parse subaccount ID
    subaccount_id = -1
    for resource in action["resources"]:
        if resource["type"] == "storage_box_subaccount":
            subaccount_id = resource["id"]
    if subaccount_id == -1:
        raise ValueError("Could not find created subaccount ID!")

    # Fetch subaccount data (needed for SSH URL)
    subaccount_url = hetzner_box_url("subaccounts", str(subaccount_id))
    response = requests.get(subaccount_url, headers=get_hetzner_headers(), timeout=60)
    response.raise_for_status()
    return response.json()["subaccount"]


def modify_storage_subaccount(subaccount_id: int, data: SubaccountInfoDict) -> None:
    """Update account on the service."""
    # Update metadata
    subaccount_url = hetzner_box_url("subaccounts", str(subaccount_id))
    response = requests.put(
        subaccount_url,
        json={"labels": data["labels"], "description": data["description"]},
        headers=get_hetzner_headers(),
        timeout=60,
    )
    response.raise_for_status()

    # Update access
    access_url = hetzner_box_url(
        "subaccounts", str(subaccount_id), "actions", "update_access_settings"
    )
    access_data: dict[str, str | bool] = {"home_directory": data["home_directory"]}
    access_data.update(cast("dict[str, bool]", data["access_settings"]))
    response = requests.post(
        access_url, json=access_data, headers=get_hetzner_headers(), timeout=60
    )
    response.raise_for_status()
    wait_for_action(response.json()["action"])


def generate_ssh_url(data: SubaccountDict) -> str:
    """Generate SSH URL from subaccount service data."""
    return "ssh://{}@{}:23/./backups".format(data["username"], data["server"])


def get_storage_subaccounts() -> list[SubaccountDict]:
    """List current subaccounts."""
    url = hetzner_box_url("subaccounts")
    response = requests.get(
        url,
        headers=get_hetzner_headers(),
        timeout=60,
    )
    response.raise_for_status()
    return response.json()["subaccounts"]

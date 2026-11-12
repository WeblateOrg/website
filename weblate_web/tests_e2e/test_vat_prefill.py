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

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from playwright.sync_api import expect

from weblate_web.payments.forms import CustomerForm

if TYPE_CHECKING:
    from playwright.sync_api import Page, Route

pytestmark = pytest.mark.django_db(transaction=True)


def open_vat_form(page: Page) -> list[str | None]:
    requests = []

    def lookup(route: Route) -> None:
        requests.append(route.request.post_data)
        route.fulfill(
            json={
                "valid": True,
                "name": "Example company",
                "address": "Example street 1\n12345  Example city",
            }
        )

    page.route("http://vat.test/js/vat/", lookup)
    page.route(
        "http://vat.test/",
        lambda route: route.fulfill(
            body=(
                '<button class="menu-show"></button>'
                '<button class="open-langs"></button>'
                '<input name="csrfmiddlewaretoken" value="test">'
                f"{CustomerForm().as_p()}"
            ),
            content_type="text/html",
        ),
    )
    page.goto("http://vat.test/")
    page.add_script_tag(path=str(Path(__file__).parents[1] / "static" / "custom.js"))
    return requests


@pytest.mark.parametrize("initial_country", ["", "FR"])
@pytest.mark.parametrize(
    ("vat", "country"),
    [("de 136695976", "DE"), ("EL123456789", "GR"), ("ATU12345678", "AT")],
)
def test_vat_input_selects_country_and_prefills(
    page: Page, vat: str, country: str, initial_country: str
) -> None:
    requests = open_vat_form(page)
    page.locator("#id_country").select_option(initial_country)
    page.locator("#id_vat").fill(vat)
    expect(page.locator("#id_country")).to_have_value(country)
    expect(page.locator("#id_name")).to_have_value("Example company")
    expect(page.locator("#id_address")).to_have_value("Example street 1")
    expect(page.locator("#id_postcode")).to_have_value("12345")
    expect(page.locator("#id_city")).to_have_value("Example city")
    expect(page.locator("#id_vat")).to_be_focused()
    assert len(requests) == 1
    assert requests[0] is not None
    assert f"\r\n{vat.upper().replace(' ', '')}\r\n" in requests[0]

    # Blurring the pasted value must not duplicate the request.
    page.locator("#id_name").focus()
    assert len(requests) == 1

    # A non-EU identifier keeps its formatting and does not trigger VIES.
    page.locator("#id_country").select_option("CH")
    page.locator("#id_vat").fill("CHE-102.321.467 MWST")
    page.locator("#id_name").focus()
    expect(page.locator("#id_country")).to_have_value("CH")
    expect(page.locator("#id_vat")).to_have_value("CHE-102.321.467 MWST")
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("country", "vat"),
    [
        ("MX", "DEL123456ABC"),
        ("MX", "DE136695976"),
        ("CH", "CHE-102.321.467 MWST"),
        ("", "DEL123456ABC"),
        ("", "DE123"),
        ("", "GB123456789"),
    ],
)
def test_vat_input_skips_non_eu_and_incomplete_ids(
    page: Page, country: str, vat: str
) -> None:
    requests = open_vat_form(page)
    page.clock.install()
    page.locator("#id_country").select_option(country)
    page.locator("#id_vat").fill(vat)
    page.clock.run_for(350)
    page.locator("#id_name").focus()
    page.wait_for_load_state("networkidle")
    expect(page.locator("#id_country")).to_have_value(country)
    expect(page.locator("#id_vat")).to_have_value(vat)
    expect(page.locator("#id_name")).to_have_value("")
    assert not requests

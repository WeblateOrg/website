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

from django.urls import path

from .views import (
    CustomerDetailView,
    CustomerListView,
    IndexView,
    InteractionDetailView,
    InteractionDownloadView,
    InvoiceListView,
    ServiceDetailView,
    ServiceListView,
)

urlpatterns = [
    path("", IndexView.as_view(), name="index"),
    path("services/<slug:kind>/", ServiceListView.as_view(), name="service-list"),
    path(
        "services/detail/<int:pk>/", ServiceDetailView.as_view(), name="service-detail"
    ),
    path("invoices/<slug:kind>/", InvoiceListView.as_view(), name="invoice-list"),
    path(
        "customers/<slug:kind>/",
        CustomerListView.as_view(),
        name="customer-list",
    ),
    path(
        "customers/detail/<int:pk>/",
        CustomerDetailView.as_view(),
        name="customer-detail",
    ),
    path(
        "interaction/<int:pk>/view/",
        InteractionDetailView.as_view(),
        name="interaction-detail",
    ),
    path(
        "interaction/<int:pk>/download/",
        InteractionDownloadView.as_view(),
        name="interaction-download",
    ),
]

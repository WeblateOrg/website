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

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Barrier
from unittest.mock import patch

from dateutil.relativedelta import relativedelta
from django.contrib.auth.models import User
from django.db import DataError, IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase, skipUnlessDBFeature
from django.utils import timezone
from lxml import html

from weblate_web.activity import record_activity
from weblate_web.forms import SupportReportForm
from weblate_web.management.commands.backups_sync import Command as BackupsSyncCommand
from weblate_web.models import (
    Package,
    PackageCategory,
    Report,
    Service,
    ServiceKind,
)
from weblate_web.payments.models import Customer, CustomerFollowUp


class ActivityTest(TestCase):
    def setUp(self) -> None:
        super().setUp()
        self.now = datetime(2026, 9, 10, tzinfo=UTC)
        self.clock = patch("django.utils.timezone.now", return_value=self.now)
        self.mock_now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.customer = Customer.objects.create(user_id=-1, origin="activity")
        self.package = Package.objects.create(
            name="extended",
            verbose="Extended support",
            price=42,
            category=PackageCategory.PACKAGE_SUPPORT,
        )
        Package.objects.create(name="community", verbose="Community support", price=0)
        self.service = Service.objects.create(
            customer=self.customer, site_url="https://example.com"
        )
        self.subscription = self.service.subscription_set.create(
            package=self.package, expires=self.now + timedelta(days=365)
        )
        self.months = [
            date(2026, 8, 1) - relativedelta(months=offset)
            for offset in range(11, -1, -1)
        ]

    def post_activity(self, activity, **extra):
        return self.client.post(
            "/api/support/",
            {
                "secret": self.service.secret,
                "site_url": "https://example.com",
                "activity": json.dumps(activity),
                **extra,
            },
            headers={"user-agent": "Weblate/2026.10"},
        )

    def payload(self, counts):
        return [
            {"year": month.year, "month": month.month, "changes": changes}
            for month, changes in zip(self.months, counts, strict=True)
        ]

    def record(self, counts):
        report = Report.objects.create(
            service=self.service, site_url="https://example.com"
        )
        record_activity(report, dict(zip(self.months, counts, strict=True)))
        return report

    def record_history(self, counts):
        latest = timezone.localdate().replace(day=1) - relativedelta(months=1)
        self.months = [
            latest - relativedelta(months=offset)
            for offset in range(len(counts) - 1, -1, -1)
        ]
        return self.record(counts)

    def test_storage_and_corrections(self) -> None:
        months = [
            date(2024, 9, 1) + relativedelta(months=offset) for offset in range(24)
        ]
        payload = [
            {"year": month.year, "month": month.month, "changes": 100}
            for month in months
        ]
        self.assertEqual(self.post_activity(payload).status_code, 200)
        self.mock_now.return_value = self.now + relativedelta(months=1)
        payload = [*payload[1:], {"year": 2026, "month": 9, "changes": 0}]
        payload[0]["changes"] = 42
        self.assertEqual(self.post_activity(payload).status_code, 200)
        self.assertEqual(self.post_activity(payload).status_code, 200)
        self.assertEqual(self.service.activity.count(), 25)
        self.assertEqual(self.service.activity.get(month=months[0]).changes, 100)
        self.assertEqual(self.service.activity.get(month=months[1]).changes, 42)
        self.assertEqual(self.service.activity.get(month=date(2026, 9, 1)).changes, 0)
        empty: list[object] | None
        for empty in (None, []):
            self.assertEqual(self.post_activity(empty).status_code, 200)
            self.assertEqual(self.service.activity.count(), 25)
        response = self.client.post(
            "/api/support/",
            {"secret": self.service.secret},
            headers={"user-agent": "Weblate/5.0"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.service.activity.count(), 25)

    def test_validation(self) -> None:
        good = {"year": 2026, "month": 8, "changes": 10}
        invalid = [
            {},
            0,
            False,
            "invalid",
            [good, good],
            [good] * 25,
            [{**good, "changes": -1}],
            [{**good, "changes": True}],
            [{**good, "changes": 1.5}],
            [{**good, "changes": 2**63}],
            [{**good, "month": 13}],
            [{**good, "year": 0}],
            [{**good, "month": 9}],
            [{**good, "year": 2027}],
            [{"year": 2026}],
            [{**good, "extra": 1}],
        ]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertEqual(self.post_activity(payload).status_code, 400)
        self.assertFalse(SupportReportForm({"activity": "{"}).is_valid())
        self.assertFalse(self.service.activity.exists())
        self.assertFalse(self.service.report_set.exists())

    def test_locked_url(self) -> None:
        self.service.site_url_lock = True
        self.service.save(update_fields=["site_url_lock"])
        response = self.post_activity(
            self.payload([100] * 9 + [0, 0, 0]), site_url="https://wrong.example.com"
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.service.activity.exists())
        self.assertFalse(
            self.service.followups.filter(
                type=CustomerFollowUp.Type.ACTIVITY_DROP
            ).exists()
        )

    def test_drop_boundaries(self) -> None:
        for counts, expected in (
            ([100] * 9 + [50, 50, 50], True),
            ([99] * 9 + [0, 0, 0], False),
            ([100] * 9 + [51, 0, 0], False),
            ([100] * 9 + [0, 0, 51], False),
            ([100] * 9 + [0, 51, 0], False),
            ([100] * 10 + [0, 0], False),
            ([0] * 12, False),
            ([100] * 8 + [101, 50, 50, 50], True),
        ):
            with self.subTest(counts=counts):
                self.service.followups.all().delete()
                Service.objects.filter(pk=self.service.pk).update(
                    activity_drop_state={}
                )
                self.record(counts)
                self.assertEqual(self.service.followups.exists(), expected)

    def test_baseline_history_limits(self) -> None:
        self.record_history([100] * 8 + [0, 0, 0])
        self.assertFalse(self.service.followups.exists())
        self.record_history([100] * 9 + [0, 0, 0])
        self.assertTrue(self.service.followups.exists())

    def test_baseline_uses_latest_twelve_months(self) -> None:
        self.record_history([10000] * 9 + [100] * 12 + [50] * 3)
        details = self.service.followups.get().details
        self.assertEqual(len(details["baseline_months"]), 12)
        self.assertEqual(details["baseline_median"], 100)
        self.assertEqual(details["threshold"], 50)
        self.assertEqual(
            details["baseline_months"][0]["month"], self.months[9].isoformat()
        )

    def test_missing_baseline_month_stops_history(self) -> None:
        counts = [100] * 15 + [0, 0, 0]
        latest = self.months[-1]
        self.months = [latest - relativedelta(months=i) for i in range(17, -1, -1)]
        payload = self.payload(counts)
        # Five contiguous baseline months remain; older months cannot fill the gap.
        self.assertEqual(
            self.post_activity(payload[:9] + payload[10:]).status_code, 200
        )
        self.assertFalse(self.service.followups.exists())
        self.assertEqual(self.post_activity(payload[9:10]).status_code, 200)
        self.assertTrue(self.service.followups.exists())

    def test_startup_spikes_are_excluded(self) -> None:
        self.record_history([0] * 4 + [1, 20000, 30000] + [100] * 6 + [51] * 3)
        self.assertFalse(self.service.followups.exists())
        self.record_history([0] * 4 + [1, 20000, 30000] + [100] * 6 + [50] * 3)
        details = self.service.followups.get().details
        self.assertEqual(details["baseline_median"], 100)
        self.assertEqual(len(details["baseline_months"]), 6)

    def test_setup_spike_does_not_inflate_threshold(self) -> None:
        self.record_history([100] * 8 + [1000000] + [51] * 3)
        self.assertFalse(self.service.followups.exists())
        self.record_history([100] * 8 + [1000000] + [50] * 3)
        details = self.service.followups.get().details
        self.assertEqual(details["baseline_median"], 100)
        self.assertEqual(details["lower_quartile"], 100)
        self.assertEqual(details["threshold"], 50)
        self.assertEqual(details["low_months"][0]["decrease_percent"], 50)

    def test_lower_quartile_accounts_for_usual_quiet_months(self) -> None:
        baseline = [100] * 3 + [20, 20, 100, 100, 100, 100]
        self.record_history(baseline + [21] * 3)
        self.assertFalse(self.service.followups.exists())
        self.record_history(baseline + [20] * 3)
        details = self.service.followups.get().details
        self.assertEqual(details["baseline_median"], 100)
        self.assertEqual(details["lower_quartile"], 40)
        self.assertEqual(details["threshold"], 20)

    def test_usually_inactive_history(self) -> None:
        self.record_history([100] * 3 + [0, 0, 0, 200, 200, 200] + [0] * 3)
        self.assertFalse(self.service.followups.exists())

    def test_chart_regressions(self) -> None:
        histories = (
            [
                2925,
                66015,
                5385,
                6659,
                6016,
                6043,
                1338,
                516,
                691,
                4028,
                31106,
                2070,
                594,
                20382,
                16532,
                5867,
                22199,
                46088,
                7270,
                23305,
                15523,
                2748,
                1544,
                2191,
            ],
            [
                216,
                2,
                0,
                2,
                10262,
                7576,
                535,
                0,
                1,
                231,
                0,
                52118,
                52,
                2262,
                2291,
                1120,
                146,
                368,
                439,
                36,
                249,
                309,
                85,
                93,
            ],
            [0] * 19 + [7, 20226, 2056, 1701, 437],
            [0] * 16 + [3915718, 2569, 53279, 352444, 1331201, 431907, 105216, 250730],
            [
                366,
                264,
                214,
                96,
                83,
                184,
                339,
                138,
                171,
                662,
                376,
                312,
                300,
                275,
                61,
                154,
                145,
                247,
                225,
                618,
                369,
                145,
                93,
                142,
            ],
            [0] * 6
            + [
                4,
                17703,
                111,
                256,
                230,
                1234,
                215,
                258,
                47,
                734,
                420,
                1332,
                374,
                503,
                1607,
                2671,
                222,
                515,
            ],
            [
                64,
                2,
                39,
                95,
                66,
                3188,
                31,
                484,
                117817,
                6517,
                279,
                2345,
                428,
                48,
                459,
                19,
                49,
                56,
                18,
                468,
                402,
                344,
                19,
                129,
            ],
        )
        for chart, counts in enumerate(histories, start=1):
            self.service.activity.all().delete()
            for index in range(len(counts)):
                with self.subTest(chart=chart, month=index):
                    self.mock_now.return_value = datetime(
                        2024, 11, 10, tzinfo=UTC
                    ) + relativedelta(months=index)
                    self.record_history(counts[: index + 1])
                    self.assertFalse(self.service.followups.exists())
                    self.service.refresh_from_db()
                    self.assertEqual(self.service.activity_drop_state, {})

    def test_missing_month_is_not_zero(self) -> None:
        payload = self.payload([100] * 9 + [0, 0, 0])
        self.assertEqual(
            self.post_activity(payload[:10] + payload[11:]).status_code, 200
        )
        self.assertFalse(self.service.followups.exists())
        self.assertEqual(self.post_activity(payload[10:11]).status_code, 200)
        self.assertTrue(self.service.followups.exists())

    def test_episode_lifecycle(self) -> None:
        self.record([100] * 9 + [50, 0, 0])
        followup = self.service.followups.get()
        followup.note = "Call next week"
        followup.follow_up_at = self.now + timedelta(days=7)
        followup.save()
        self.record([100] * 9 + [0, 0, 0])
        followup.refresh_from_db()
        self.assertEqual(followup.note, "Call next week")
        self.assertEqual(followup.follow_up_at, self.now + timedelta(days=7))
        # Corrected historical counts do not count as recovery.
        self.record([100] * 9 + [0, 0, 100])
        self.assertEqual(self.service.followups.get().pk, followup.pk)
        followup.delete()
        self.record([100] * 9 + [0, 0, 0])
        self.assertFalse(self.service.followups.exists())
        # A shrinking rolling baseline must not rearm the episode.
        self.mock_now.return_value = self.now + relativedelta(months=3)
        self.months = [month + relativedelta(months=3) for month in self.months]
        self.record([0] * 12)
        self.service.refresh_from_db()
        self.assertIn("baseline_sum", self.service.activity_drop_state)
        self.assertEqual(self.service.activity_drop_state["threshold"], 50)
        self.record([0] * 11 + [51])
        self.service.refresh_from_db()
        self.assertIn("recovered_month", self.service.activity_drop_state)
        self.mock_now.return_value += relativedelta(months=5)
        self.months = [month + relativedelta(months=5) for month in self.months]
        self.record([100] * 9 + [0, 0, 0])
        self.assertTrue(self.service.followups.exists())

    def test_backup_removal_preserves_episode(self) -> None:
        self.service.backup_repository = "backup-repository"
        self.service.backup_box = 42
        self.service.backup_directory = "test-backup"
        self.service.backup_subaccount = 123
        self.service.save()
        for state in (
            {"baseline_sum": 300, "detected_month": "2026-08-01"},
            {"recovered_month": "2026-09-01"},
        ):
            with self.subTest(state=state):
                # The backup command loaded this service before the report's update.
                Service.objects.filter(pk=self.service.pk).update(
                    activity_drop_state=state
                )
                with (
                    patch(
                        "weblate_web.management.commands.backups_sync.delete_storage_subaccount"
                    ),
                    patch("weblate_web.management.commands.backups_sync.sftp_client"),
                    patch(
                        "weblate_web.management.commands.backups_sync.remove_directory"
                    ),
                ):
                    BackupsSyncCommand().remove_disabled(
                        {"backup-repository": self.service}
                    )
                updated = Service.objects.get(pk=self.service.pk)
                self.assertEqual(updated.activity_drop_state, state)
                self.assertEqual(updated.backup_repository, "")
                self.assertEqual(updated.backup_removed["directory"], "test-backup")
                self.service.backup_repository = "backup-repository"
                self.service.backup_box = 42
                self.service.backup_directory = "test-backup"
                self.service.backup_subaccount = 123

    def test_local_month_boundaries(self) -> None:
        for zone, now, latest in (
            (
                "Europe/Prague",
                datetime(2026, 8, 31, 22, 30, tzinfo=UTC),
                date(2026, 8, 1),
            ),
            (
                "Europe/Prague",
                datetime(2026, 12, 31, 23, 30, tzinfo=UTC),
                date(2026, 12, 1),
            ),
            (
                "America/Los_Angeles",
                datetime(2026, 9, 1, 1, tzinfo=UTC),
                date(2026, 7, 1),
            ),
        ):
            with self.subTest(zone=zone, now=now), timezone.override(zone):
                self.mock_now.return_value = now
                self.service.activity.all().delete()
                self.service.followups.all().delete()
                Service.objects.filter(pk=self.service.pk).update(
                    activity_drop_state={}
                )
                self.months = [
                    latest - relativedelta(months=offset)
                    for offset in range(11, -1, -1)
                ]
                self.assertEqual(
                    self.post_activity(
                        self.payload([100] * 9 + [50, 50, 50]), discoverable="1"
                    ).status_code,
                    200,
                )
                self.assertEqual(
                    self.service.followups.get().details["detected_month"],
                    latest.isoformat(),
                )
                current = latest + relativedelta(months=1)
                self.assertEqual(
                    self.post_activity(
                        [{"year": current.year, "month": current.month, "changes": 10}]
                    ).status_code,
                    400,
                )
                competitor = Service.objects.create(
                    customer=self.customer, discoverable=True
                )
                competitor.activity.create(month=latest, changes=10)
                competitor.activity.create(
                    month=latest - relativedelta(months=1), changes=1000
                )
                response = self.client.get("/en/discover/")
                self.assertEqual(
                    [
                        service.pk
                        for service in response.context["discoverable_services"]
                    ],
                    [self.service.pk, competitor.pk],
                )
                competitor.delete()

    def test_recovery_clears_open_followup(self) -> None:
        self.record([100] * 9 + [0, 0, 0])
        self.mock_now.return_value += relativedelta(months=1)
        self.months = [month + relativedelta(months=1) for month in self.months]
        self.record([100] * 8 + [0, 0, 0, 50])
        self.assertTrue(self.service.followups.exists())
        self.record([100] * 8 + [0, 0, 0, 51])
        self.assertFalse(self.service.followups.exists())

    def test_legacy_episode_preserves_recovery_rule(self) -> None:
        state = {
            "service_id": self.service.pk,
            "detected_month": "2026-08-01",
            "baseline_sum": 300,
            "baseline_average": 100,
            "baseline_months": [
                {"month": f"2026-{month:02d}-01", "changes": 100}
                for month in range(4, 7)
            ],
            "low_months": [
                {"month": f"2026-{month:02d}-01", "changes": 0, "decrease_percent": 100}
                for month in range(7, 9)
            ],
            "latest_month": "2026-08-01",
            "latest_changes": 0,
        }
        for cleared in (False, True):
            with self.subTest(cleared=cleared):
                Service.objects.filter(pk=self.service.pk).update(
                    activity_drop_state=state
                )
                if not cleared:
                    followup = self.service.followups.create(
                        customer=self.customer,
                        type=CustomerFollowUp.Type.ACTIVITY_DROP,
                        follow_up_at=self.now + timedelta(days=7),
                        note="Already reviewed",
                        details=state,
                    )
                self.record([100] * 9 + [0] * 3)
                self.service.refresh_from_db()
                self.assertEqual(self.service.activity_drop_state, state)
                self.assertEqual(self.service.followups.exists(), not cleared)
                if not cleared:
                    followup.refresh_from_db()
                    self.assertEqual(followup.note, "Already reviewed")
                    self.assertEqual(
                        followup.follow_up_at, self.now + timedelta(days=7)
                    )
                    self.assertNotIn("detector_version", followup.details)
                self.mock_now.return_value = self.now + relativedelta(months=1)
                self.record_history([100] * 8 + [0, 0, 0, 50])
                self.assertEqual(self.service.followups.exists(), not cleared)
                self.record_history([100] * 8 + [0, 0, 0, 51])
                self.assertFalse(self.service.followups.exists())
                self.service.refresh_from_db()
                self.assertEqual(
                    self.service.activity_drop_state, {"recovered_month": "2026-09-01"}
                )
                self.mock_now.return_value = self.now
                self.months = [month - relativedelta(months=1) for month in self.months]

    def test_legacy_crm_baseline(self) -> None:
        self.record([100] * 9 + [0] * 3)
        followup = self.service.followups.get()
        followup.details.pop("detector_version")
        followup.details.pop("baseline_median")
        followup.details.pop("lower_quartile")
        followup.details.pop("threshold")
        followup.save(update_fields=["details"])
        user = User.objects.create_superuser(
            "legacy-admin", "admin@example.com", "test-password"
        )
        self.client.force_login(user)
        response = self.client.get(self.customer.get_absolute_url())
        self.assertContains(response, "Baseline: 100.0 changes per month.")
        self.assertNotContains(response, "Median baseline:")
        self.assertNotContains(response, "Alert threshold:")

    def test_status_update_preserves_episode(self) -> None:
        self.record([100] * 9 + [0, 0, 0])
        self.service.followups.all().delete()
        # Simulate a concurrent report holding a service loaded before the drop.
        self.service.status = "community"
        self.service.update_status()
        self.record([100] * 9 + [0, 0, 0])
        self.assertFalse(self.service.followups.exists())
        self.service.refresh_from_db()
        self.assertIn("baseline_sum", self.service.activity_drop_state)

    def test_eligibility(self) -> None:
        for enabled, expired, category, name, kind, expected in (
            (
                True,
                False,
                PackageCategory.PACKAGE_DEDICATED,
                "hosted:test",
                ServiceKind.SERVICE,
                True,
            ),
            (
                True,
                False,
                PackageCategory.PACKAGE_SHARED,
                "shared:test",
                ServiceKind.SERVICE,
                True,
            ),
            (
                True,
                False,
                PackageCategory.PACKAGE_NONE,
                "backup",
                ServiceKind.SERVICE,
                True,
            ),
            (
                False,
                False,
                PackageCategory.PACKAGE_SUPPORT,
                "extended",
                ServiceKind.SERVICE,
                False,
            ),
            (
                True,
                True,
                PackageCategory.PACKAGE_SUPPORT,
                "extended",
                ServiceKind.SERVICE,
                False,
            ),
            (
                True,
                False,
                PackageCategory.PACKAGE_DONATION,
                "donation",
                ServiceKind.DONATION,
                False,
            ),
            (
                True,
                False,
                PackageCategory.PACKAGE_NONE,
                "community-test",
                ServiceKind.SERVICE,
                False,
            ),
        ):
            with self.subTest(name=name, enabled=enabled, expired=expired):
                self.service.followups.all().delete()
                Service.objects.filter(pk=self.service.pk).update(
                    activity_drop_state={}, kind=kind
                )
                self.subscription.enabled = enabled
                self.subscription.expires = self.now + timedelta(
                    days=-1 if expired else 365
                )
                self.subscription.save()
                self.package.category = category
                self.package.name = name
                self.package.save()
                self.record([100] * 9 + [0, 0, 0])
                self.assertEqual(
                    self.service.followups.filter(
                        type=CustomerFollowUp.Type.ACTIVITY_DROP
                    ).exists(),
                    expected,
                )
        self.subscription.delete()
        self.record([100] * 9 + [0, 0, 0])
        self.assertFalse(
            self.service.followups.filter(
                type=CustomerFollowUp.Type.ACTIVITY_DROP
            ).exists()
        )

    def test_recovery_during_reporting_gap(self) -> None:
        self.record([100] * 9 + [0, 0, 0])
        original = self.service.followups.get()
        self.mock_now.return_value += relativedelta(months=5)
        self.months = [month + relativedelta(months=5) for month in self.months]
        # The next report contains recovery followed by another sustained drop.
        self.record([100] * 9 + [0, 0, 0])
        self.assertNotEqual(self.service.followups.get().pk, original.pk)

    def test_storage_is_atomic(self) -> None:
        report = Report.objects.create(service=self.service)
        with (
            patch(
                "weblate_web.activity.reconcile_activity_drop",
                side_effect=RuntimeError("failure"),
            ),
            self.assertRaises(RuntimeError),
        ):
            record_activity(
                report, dict(zip(self.months, [100] * 9 + [0, 0, 0], strict=True))
            )
        self.assertFalse(self.service.activity.exists())
        self.assertFalse(self.service.followups.exists())
        self.service.refresh_from_db()
        self.assertEqual(self.service.activity_drop_state, {})

    def test_constraints(self) -> None:
        self.service.activity.create(month=date(2026, 8, 1), changes=0)
        for month, changes in (
            (date(2026, 8, 1), 1),
            (date(2026, 7, 2), 1),
        ):
            with (
                self.subTest(month=month, changes=changes),
                self.assertRaises(IntegrityError),
                transaction.atomic(),
            ):
                self.service.activity.create(month=month, changes=changes)
        # MariaDB rejects unsigned values with DataError rather than a CHECK error.
        with self.assertRaises((IntegrityError, DataError)), transaction.atomic():
            self.service.activity.create(month=date(2026, 7, 1), changes=-1)

    @skipUnlessDBFeature("supports_partial_indexes")
    def test_followup_constraint(self) -> None:
        # Automatic report deduplication is tested on every locking backend below.
        self.record([100] * 9 + [0, 0, 0])
        with self.assertRaises(IntegrityError), transaction.atomic():
            CustomerFollowUp.objects.create(
                service=self.service,
                customer=self.customer,
                type=CustomerFollowUp.Type.ACTIVITY_DROP,
                follow_up_at=timezone.now(),
            )

    def test_crm_and_clearing(self) -> None:
        self.record([100] * 9 + [50, 0, 0])
        followup = self.service.followups.get()
        user = User.objects.create_superuser(
            "activity-admin", "admin@example.com", "test-password"
        )
        self.client.force_login(user)
        response = self.client.get(self.customer.get_absolute_url())
        self.assertContains(
            response, "Contact the customer to understand the activity decrease."
        )
        self.assertContains(response, "Median baseline: 100.0 changes per month.")
        self.assertContains(response, "Alert threshold: 50.0 changes per month.")
        self.assertContains(response, "50.0% decrease")
        self.assertContains(response, self.service.get_absolute_url())
        # Template loops render no text nodes: the activity list contains only li.
        document = html.fromstring(response.content)
        activity_lists = document.xpath("//ul[li[contains(., 'changes')]]")
        self.assertEqual(len(activity_lists), 1)
        activity_list = activity_lists[0]
        self.assertEqual([child.tag for child in activity_list], ["li"] * 9)
        self.assertFalse((activity_list.text or "").strip())
        self.assertTrue(all(not (child.tail or "").strip() for child in activity_list))
        response = self.client.post(
            self.customer.get_absolute_url(),
            {"clear_follow_up": "1", "follow_up": followup.pk},
        )
        self.assertEqual(response.status_code, 302)
        self.record([100] * 9 + [0, 0, 0])
        self.assertFalse(self.service.followups.exists())


class ActivityConcurrencyTest(TransactionTestCase):
    @skipUnlessDBFeature("has_select_for_update")
    def test_concurrent_reports(self) -> None:
        customer = Customer.objects.create(user_id=-1, origin="activity")
        package = Package.objects.create(
            name="extended",
            verbose="Support",
            price=42,
            category=PackageCategory.PACKAGE_SUPPORT,
        )
        Package.objects.create(name="community", verbose="Community support", price=0)
        service = Service.objects.create(customer=customer)
        service.subscription_set.create(
            package=package, expires=timezone.now() + timedelta(days=365)
        )
        report = Report.objects.create(service=service)
        latest = timezone.localdate().replace(day=1) - relativedelta(months=1)
        months = [latest - relativedelta(months=offset) for offset in range(11, -1, -1)]
        activity = dict(zip(months, [100] * 9 + [0, 0, 0], strict=True))
        barrier = Barrier(2)

        def submit(_index: int) -> None:
            try:
                thread_report = Report.objects.get(pk=report.pk)
                barrier.wait(timeout=10)
                record_activity(thread_report, activity)
            finally:
                connection.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(submit, range(2)))
        self.assertEqual(service.activity.count(), 12)
        self.assertEqual(service.followups.count(), 1)


class ActivityDiscoveryTest(TransactionTestCase):
    # MariaDB full-text searches only see committed project rows.
    @patch("django.utils.timezone.now", return_value=datetime(2026, 9, 10, tzinfo=UTC))
    def test_discover_order(self, mock_now) -> None:
        customer = Customer.objects.create(user_id=-1, origin="activity")
        services = []
        for title, changes in (
            ("Unknown", None),
            ("Zero", 0),
            ("B", 10),
            ("A", 10),
            ("Most active", 100),
        ):
            service = Service.objects.create(
                customer=customer, discoverable=True, site_title=title
            )
            service.project_set.create(
                name="Matching project",
                url="/projects/matching/",
                web="https://example.com",
            )
            if changes is not None:
                service.activity.create(month=date(2026, 8, 1), changes=changes)
            services.append(service)
        services[0].activity.create(month=date(2026, 7, 1), changes=1000)
        for query in ("", "?q=matching"):
            response = self.client.get(f"/en/discover/{query}")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                [service.pk for service in response.context["discoverable_services"]],
                [service.pk for service in reversed(services)],
            )
        mock_now.return_value = datetime(2027, 1, 5, tzinfo=UTC)
        services[0].activity.create(month=date(2026, 12, 1), changes=1)
        response = self.client.get("/en/discover/")
        self.assertEqual(
            next(iter(response.context["discoverable_services"])), services[0]
        )

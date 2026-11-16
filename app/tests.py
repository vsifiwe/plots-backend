import json
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from unittest.mock import patch
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import status
from rest_framework.test import APISimpleTestCase, APITestCase

from .models import (
    Asset,
    AssetCommitment,
    AssetValuation,
    Contribution,
    Member,
    RNITNav,
    RNITPurchase,
)


FIXED_TODAY = date(2026, 9, 12)


def aware_datetime(year, month, day):
    return timezone.make_aware(datetime(year, month, day, 12, 0))


class MemberAdminTests(TestCase):
    def test_member_change_page_displays_join_date_and_saves_edits(self):
        steward = Member.objects.create_superuser(
            email="admin@example.com", password="admin-test-password", full_name="Admin"
        )
        member = Member.objects.create_user(
            phone="0781234567", password="member-test-password", full_name="Member"
        )
        self.client.force_login(steward)
        url = reverse("admin:app_member_change", args=[member.pk])

        response = self.client.get(url)

        self.assertContains(response, "Date joined")
        self.assertNotIn("date_joined", response.context["adminform"].form.fields)

        response = self.client.post(url, {
            "full_name": "Updated Member",
            "phone": member.phone,
            "role": member.role,
            "is_active": "on",
            "_save": "Save",
        })

        self.assertRedirects(response, reverse("admin:app_member_changelist"))
        date_joined = member.date_joined
        password_hash = member.password
        member.refresh_from_db()
        self.assertEqual(member.full_name, "Updated Member")
        self.assertEqual(member.date_joined, date_joined)
        self.assertEqual(member.password, password_hash)


class RNITPublishedNavTests(APISimpleTestCase):
    url = "/api/savings/navs/published/"

    @staticmethod
    def feed_response(dates):
        return BytesIO(json.dumps({
            "ok": True,
            "entries": [{"nav_date": day, "nav_value": "286.7500"} for day in dates],
        }).encode())

    @patch("app.rnit.urlopen")
    def test_only_stewards_can_request_published_navs(self, urlopen):
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_401_UNAUTHORIZED)
        self.client.force_authenticate(Member(role=Member.Role.MEMBER))
        self.assertEqual(self.client.get(self.url).status_code, status.HTTP_403_FORBIDDEN)
        urlopen.assert_not_called()

    @patch("app.views.timezone.localdate", return_value=date(2027, 1, 5))
    @patch("app.rnit.urlopen")
    def test_latest_seven_can_span_years(self, urlopen, _localdate):
        self.client.force_authenticate(Member(role=Member.Role.STEWARD))
        urlopen.side_effect = [
            self.feed_response(["2027-01-05", "2027-01-04", "2027-01-06", "2027-01-05"]),
            self.feed_response([f"2026-12-{day}" for day in range(23, 32)]),
        ]

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        dates = ["2027-01-05", "2027-01-04"] + [f"2026-12-{day}" for day in range(31, 26, -1)]
        self.assertEqual(
            response.json(),
            [{"as_of_date": day, "nav_per_unit": "286.7500"} for day in dates],
        )
        requests = [call.args[0] for call in urlopen.call_args_list]
        self.assertEqual(
            [parse_qs(urlsplit(request.full_url).query) for request in requests],
            [{"history": ["1"], "year": ["2027"]}, {"history": ["1"], "year": ["2026"]}],
        )
        self.assertTrue(all(request.get_header("User-agent") for request in requests))

    @patch("app.rnit.urlopen")
    def test_feed_failures_return_bad_gateway(self, urlopen):
        self.client.force_authenticate(Member(role=Member.Role.STEWARD))
        urlopen.side_effect = [URLError("Offline"), BytesIO(b"not JSON")]

        for _ in range(2):
            response = self.client.get(self.url)
            self.assertEqual(response.status_code, status.HTTP_502_BAD_GATEWAY)


class OverviewTestCase(APITestCase):
    def create_member(self, name, *, active=True, role=Member.Role.MEMBER):
        slug = name.lower().replace(" ", ".")
        return Member.objects.create_user(
            email=f"{slug}@example.com",
            password="correct-horse-battery-staple",
            full_name=name,
            is_active=active,
            role=role,
        )

    def create_contribution(
        self,
        member,
        amount,
        received_on,
        *,
        recorded_by=None,
        voided=False,
    ):
        recorded_by = recorded_by or member
        return Contribution.objects.create(
            member=member,
            amount=amount,
            received_on=received_on,
            recorded_by=recorded_by,
            voided_at=timezone.now() if voided else None,
            voided_by=recorded_by if voided else None,
            void_reason="Duplicate record" if voided else "",
        )


class OverviewAccessTests(OverviewTestCase):
    def setUp(self):
        self.member = self.create_member("Regular Member")
        self.steward = self.create_member("Group Steward", role=Member.Role.STEWARD)

    def test_overview_requires_authentication(self):
        response = self.client.get("/api/overview/")

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @patch("app.overview.timezone.localdate", return_value=FIXED_TODAY)
    def test_members_and_stewards_receive_the_collective_overview(self, _localdate):
        self.create_contribution(self.member, 250000, date(2025, 12, 1))

        self.client.force_authenticate(self.member)
        member_response = self.client.get("/api/overview/")
        self.client.force_authenticate(self.steward)
        steward_response = self.client.get("/api/overview/")

        self.assertEqual(member_response.status_code, status.HTTP_200_OK)
        self.assertEqual(steward_response.status_code, status.HTTP_200_OK)
        self.assertEqual(member_response.data, steward_response.data)


class OverviewAggregationTests(OverviewTestCase):
    @patch("app.overview.timezone.localdate", return_value=FIXED_TODAY)
    def test_segment_boundaries_membership_and_concentration(self, _localdate):
        values = (
            ("Corner Member", 2000001, True),
            ("Growth Floor", 500000, True),
            ("Growth Ceiling", 2000000, True),
            ("Emerging Member", 499999, True),
            ("Small Member", 100000, True),
            ("Legacy Member", 700000, False),
        )
        for name, value, active in values:
            member = self.create_member(name, active=active)
            self.create_contribution(member, value, date(2025, 12, 1))

        self.client.force_authenticate(self.create_member("Viewer"))
        response = self.client.get("/api/overview/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        membership = response.data["membership"]
        self.assertEqual(membership["total_members"], 7)
        self.assertEqual(membership["active_members"], 6)
        self.assertEqual(membership["inactive_members"], 1)
        self.assertEqual(membership["average_active_portfolio"], 850000.0)
        self.assertAlmostEqual(membership["top_five_concentration"], 98.28)

        segments = {segment["key"]: segment for segment in response.data["segments"]}
        self.assertEqual(
            [member["full_name"] for member in segments["cornerstone"]["members"]],
            ["Corner Member"],
        )
        self.assertEqual(
            {member["full_name"] for member in segments["growth"]["members"]},
            {"Growth Floor", "Growth Ceiling"},
        )
        self.assertEqual(
            {member["full_name"] for member in segments["emerging"]["members"]},
            {"Emerging Member", "Small Member", "Viewer"},
        )
        self.assertEqual(
            [member["full_name"] for member in segments["legacy"]["members"]],
            ["Legacy Member"],
        )

    @patch("app.overview.timezone.localdate", return_value=FIXED_TODAY)
    def test_contribution_health_uses_active_non_voided_members(self, _localdate):
        steward = self.create_member("Steward", role=Member.Role.STEWARD)
        active_members = [
            self.create_member(f"Active {number}") for number in range(1, 5)
        ]
        inactive_member = self.create_member("Inactive Member", active=False)
        for member, amount in zip(active_members[:3], (100, 200, 300)):
            self.create_contribution(
                member, amount, date(2026, 8, 15), recorded_by=steward
            )
        self.create_contribution(
            active_members[3],
            400,
            date(2026, 8, 16),
            recorded_by=steward,
            voided=True,
        )
        self.create_contribution(
            inactive_member, 500, date(2026, 8, 17), recorded_by=steward
        )

        self.client.force_authenticate(active_members[0])
        response = self.client.get("/api/overview/")

        current = response.data["contributions"]["current_window"]
        self.assertEqual(current["month"], "2026-08-01")
        self.assertEqual(current["active_members"], 5)
        self.assertEqual(current["contributing_members"], 3)
        self.assertEqual(current["health_percentage"], 60.0)
        self.assertFalse(current["is_healthy"])
        history = response.data["contributions"]["history"]
        self.assertEqual(len(history), 8)
        self.assertEqual(history[0]["month"], "2026-01-01")
        self.assertEqual(history[-1]["total_received"], 600)

    @patch("app.overview.timezone.localdate", return_value=FIXED_TODAY)
    def test_redeemed_cash_and_units_are_replaced_by_acquired_asset_value(
        self, _localdate
    ):
        member = self.create_member("Investor")
        steward = self.create_member("Steward", role=Member.Role.STEWARD)
        cash_contribution = self.create_contribution(
            member, 1000, date(2025, 12, 1), recorded_by=steward
        )
        rnit_contribution = self.create_contribution(
            member, 1000, date(2025, 12, 2), recorded_by=steward
        )
        RNITNav.objects.create(
            as_of_date=date(2026, 6, 1),
            nav_per_unit=Decimal("100"),
            recorded_by=steward,
        )
        latest_nav = RNITNav.objects.create(
            as_of_date=date(2026, 9, 1),
            nav_per_unit=Decimal("120"),
            recorded_by=steward,
        )
        RNITPurchase.objects.create(
            contribution=rnit_contribution,
            member=member,
            purchased_on=date(2026, 5, 1),
            nav_per_unit=Decimal("100"),
            units=Decimal("10"),
            recorded_by=steward,
        )

        fixed_asset = Asset.objects.create(
            title="Land",
            total_cost=400,
            asset_type=Asset.AssetType.FIXED,
            status=Asset.Status.ACQUIRED,
            created_by=steward,
            acquired_at=aware_datetime(2026, 6, 1),
        )
        AssetCommitment.objects.create(
            asset=fixed_asset,
            member=member,
            amount=400,
            reserved_at=aware_datetime(2026, 5, 20),
            reserved_cash=400,
            redeemed_at=aware_datetime(2026, 6, 1),
            redeemed_by=steward,
        )
        AssetValuation.objects.create(
            asset=fixed_asset,
            as_of_date=date(2026, 8, 31),
            total_value=Decimal("800"),
            recorded_by=steward,
        )

        variable_asset = Asset.objects.create(
            title="Listed shares",
            total_cost=400,
            asset_type=Asset.AssetType.VARIABLE,
            status=Asset.Status.ACQUIRED,
            created_by=steward,
            acquired_at=aware_datetime(2026, 6, 1),
            acquired_units=Decimal("20"),
            acquisition_unit_price=Decimal("20"),
        )
        AssetCommitment.objects.create(
            asset=variable_asset,
            member=member,
            amount=400,
            reserved_at=aware_datetime(2026, 5, 20),
            reserved_units=Decimal("4"),
            reservation_nav=Decimal("100"),
            redeemed_at=aware_datetime(2026, 6, 1),
            redeemed_by=steward,
            redemption_nav=Decimal("100"),
        )
        AssetValuation.objects.create(
            asset=variable_asset,
            as_of_date=date(2026, 9, 1),
            unit_price=Decimal("30"),
            recorded_by=steward,
        )

        self.client.force_authenticate(member)
        response = self.client.get("/api/overview/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["valuation_as_of"], date(2026, 9, 1))
        self.assertEqual(response.data["portfolio"]["total_value"], 2720.0)
        allocation = {
            entry["key"]: entry for entry in response.data["portfolio"]["allocation"]
        }
        self.assertEqual(allocation["pending_cash"]["value"], 600.0)
        self.assertEqual(allocation["rnit"]["value"], 720.0)
        self.assertEqual(allocation["fixed_assets"]["value"], 800.0)
        self.assertEqual(allocation["variable_assets"]["value"], 600.0)
        shares = next(
            holding
            for holding in response.data["holdings"]
            if holding["id"] == variable_asset.id
        )
        self.assertEqual(shares["growth_percentage"], 50.0)
        self.assertEqual(shares["unit_price_change_percentage"], 50.0)
        self.assertEqual(shares["rnit_benchmark_percentage"], 20.0)
        self.assertEqual(cash_contribution.amount, 1000)
        self.assertEqual(latest_nav.nav_per_unit, Decimal("120"))

    @patch("app.overview.timezone.localdate", return_value=FIXED_TODAY)
    def test_open_assets_remain_in_liquid_allocation(self, _localdate):
        member = self.create_member("Investor")
        self.create_contribution(member, 1000, date(2025, 12, 1))
        asset = Asset.objects.create(
            title="Reserved land",
            total_cost=700,
            status=Asset.Status.OPEN,
            created_by=member,
        )
        AssetCommitment.objects.create(
            asset=asset,
            member=member,
            amount=700,
            reserved_at=aware_datetime(2026, 8, 1),
            reserved_cash=700,
        )

        self.client.force_authenticate(member)
        response = self.client.get("/api/overview/")

        self.assertEqual(response.data["portfolio"]["total_value"], 1000.0)
        allocation = {
            entry["key"]: entry for entry in response.data["portfolio"]["allocation"]
        }
        self.assertEqual(allocation["pending_cash"]["value"], 1000.0)
        self.assertEqual(allocation["fixed_assets"]["value"], 0.0)
        self.assertEqual(response.data["holdings"], [])

    @patch("app.overview.timezone.localdate", return_value=FIXED_TODAY)
    def test_acquisition_values_are_fallbacks_when_valuations_are_missing(
        self, _localdate
    ):
        member = self.create_member("Investor")
        fixed_asset = Asset.objects.create(
            title="Land",
            total_cost=500,
            asset_type=Asset.AssetType.FIXED,
            status=Asset.Status.ACQUIRED,
            created_by=member,
            acquired_at=aware_datetime(2026, 7, 1),
        )
        variable_asset = Asset.objects.create(
            title="Shares",
            total_cost=400,
            asset_type=Asset.AssetType.VARIABLE,
            status=Asset.Status.ACQUIRED,
            created_by=member,
            acquired_at=aware_datetime(2026, 7, 1),
            acquired_units=Decimal("20"),
            acquisition_unit_price=Decimal("20"),
        )
        for asset in (fixed_asset, variable_asset):
            AssetCommitment.objects.create(
                asset=asset,
                member=member,
                amount=asset.total_cost,
                redeemed_at=aware_datetime(2026, 7, 1),
                redeemed_by=member,
            )

        self.client.force_authenticate(member)
        response = self.client.get("/api/overview/")

        holdings = {holding["id"]: holding for holding in response.data["holdings"]}
        self.assertEqual(holdings[fixed_asset.id]["current_value"], 500.0)
        self.assertEqual(holdings[variable_asset.id]["current_value"], 400.0)
        self.assertEqual(holdings[variable_asset.id]["current_unit_price"], 20.0)
        self.assertEqual(
            holdings[variable_asset.id]["rnit_benchmark_percentage"], None
        )

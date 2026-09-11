from datetime import date, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from django.utils import timezone

from .models import (
    Asset,
    Contribution,
    Member,
    RNITNav,
    get_latest_rnit_nav,
    get_member_pending_cash,
    get_member_total_units,
)
from .serializers import asset_current_value, latest_asset_valuation


HEALTHY_CONTRIBUTION_PERCENTAGE = Decimal("75")
GROUP_TIME_ZONE = ZoneInfo("Africa/Kigali")
SEGMENT_DEFINITIONS = (
    ("cornerstone", "Cornerstone", "Over RWF 2M"),
    ("growth", "Growth", "RWF 500K-2M"),
    ("emerging", "Emerging", "Below RWF 500K"),
    ("legacy", "Legacy", "Inactive members"),
)


def _percentage(value, total):
    if not total:
        return 0.0
    return round(float(Decimal(value) * Decimal("100") / Decimal(total)), 2)


def _money(value):
    return round(float(Decimal(value)), 2)


def _shift_month(month, offset):
    month_index = month.year * 12 + month.month - 1 + offset
    return date(month_index // 12, month_index % 12 + 1, 1)


def _latest_completed_month(today):
    return (today.replace(day=1) - timedelta(days=1)).replace(day=1)


def _member_segment(member, value):
    if not member.is_active:
        return "legacy"
    if value > Decimal("2000000"):
        return "cornerstone"
    if value >= Decimal("500000"):
        return "growth"
    return "emerging"


def _holding_summary(asset, latest_nav):
    current_value = asset_current_value(asset)
    if current_value is None:
        return None

    current_value = Decimal(current_value)
    cost = Decimal(asset.total_cost)
    growth = current_value - cost
    valuation = latest_asset_valuation(asset)
    current_unit_price = None
    unit_price_change = None
    unit_price_change_percentage = None

    if asset.asset_type == Asset.AssetType.VARIABLE:
        current_unit_price = (
            valuation.unit_price if valuation else asset.acquisition_unit_price
        )
        if current_unit_price is not None and asset.acquisition_unit_price is not None:
            unit_price_change = current_unit_price - asset.acquisition_unit_price
            unit_price_change_percentage = _percentage(
                unit_price_change, asset.acquisition_unit_price
            )

    acquisition_date = asset.acquired_at.date() if asset.acquired_at else None
    benchmark_percentage = None
    if latest_nav and acquisition_date:
        starting_nav = (
            RNITNav.objects.filter(as_of_date__lte=acquisition_date)
            .order_by("-as_of_date", "-id")
            .first()
        )
        if starting_nav:
            benchmark_percentage = _percentage(
                latest_nav.nav_per_unit - starting_nav.nav_per_unit,
                starting_nav.nav_per_unit,
            )

    return {
        "id": asset.id,
        "title": asset.title,
        "asset_type": asset.asset_type,
        "cost": _money(cost),
        "current_value": _money(current_value),
        "growth": _money(growth),
        "growth_percentage": _percentage(growth, cost),
        "acquired_on": acquisition_date,
        "latest_valuation_date": valuation.as_of_date if valuation else None,
        "acquisition_units": str(asset.acquired_units)
        if asset.acquired_units is not None
        else None,
        "acquisition_unit_price": _money(asset.acquisition_unit_price)
        if asset.acquisition_unit_price is not None
        else None,
        "current_unit_price": _money(current_unit_price)
        if current_unit_price is not None
        else None,
        "unit_price_change": _money(unit_price_change)
        if unit_price_change is not None
        else None,
        "unit_price_change_percentage": unit_price_change_percentage,
        "rnit_benchmark_percentage": benchmark_percentage,
    }


def _contribution_summary(members, today):
    active_member_ids = {member.id for member in members if member.is_active}
    latest_month = _latest_completed_month(today)
    history_start = _shift_month(latest_month, -7)
    history_end = _shift_month(latest_month, 1)
    buckets = {
        _shift_month(history_start, offset): {
            "total_received": 0,
            "member_ids": set(),
        }
        for offset in range(8)
    }

    contributions = Contribution.objects.filter(
        member_id__in=active_member_ids,
        received_on__gte=history_start,
        received_on__lt=history_end,
        voided_at__isnull=True,
    ).values("member_id", "received_on", "amount")
    for contribution in contributions:
        bucket_month = contribution["received_on"].replace(day=1)
        bucket = buckets[bucket_month]
        bucket["total_received"] += contribution["amount"]
        bucket["member_ids"].add(contribution["member_id"])

    history = [
        {
            "month": month.isoformat(),
            "total_received": bucket["total_received"],
            "contributing_members": len(bucket["member_ids"]),
        }
        for month, bucket in buckets.items()
    ]
    latest_bucket = buckets[latest_month]
    active_members = len(active_member_ids)
    health_percentage = (
        round(len(latest_bucket["member_ids"]) * 100 / active_members, 2)
        if active_members
        else 0.0
    )

    return {
        "current_window": {
            "month": latest_month.isoformat(),
            "active_members": active_members,
            "contributing_members": len(latest_bucket["member_ids"]),
            "health_percentage": health_percentage,
            "healthy_threshold": float(HEALTHY_CONTRIBUTION_PERCENTAGE),
            "is_healthy": health_percentage
            >= float(HEALTHY_CONTRIBUTION_PERCENTAGE),
        },
        "history": history,
    }


def build_overview_summary():
    today = timezone.localdate(timezone=GROUP_TIME_ZONE)
    members = list(Member.objects.order_by("full_name", "id"))
    latest_nav = get_latest_rnit_nav()
    member_values = {member.id: Decimal("0") for member in members}

    pending_cash = Decimal("0")
    rnit_value = Decimal("0")
    for member in members:
        member_pending_cash = get_member_pending_cash(member.id)
        member_rnit_value = get_member_total_units(member.id) * (
            latest_nav.nav_per_unit if latest_nav else Decimal("0")
        )
        pending_cash += member_pending_cash
        rnit_value += member_rnit_value
        member_values[member.id] += member_pending_cash + member_rnit_value

    fixed_assets = Decimal("0")
    variable_assets = Decimal("0")
    holdings = []
    valuation_dates = [latest_nav.as_of_date] if latest_nav else []
    acquired_assets = Asset.objects.filter(status=Asset.Status.ACQUIRED).prefetch_related(
        "commitments__member", "valuations"
    )
    for asset in acquired_assets:
        current_value = asset_current_value(asset)
        if current_value is None:
            continue
        current_value = Decimal(current_value)
        if asset.asset_type == Asset.AssetType.VARIABLE:
            variable_assets += current_value
        else:
            fixed_assets += current_value

        valuation = latest_asset_valuation(asset)
        if valuation:
            valuation_dates.append(valuation.as_of_date)
        elif asset.acquired_at:
            valuation_dates.append(asset.acquired_at.date())

        for commitment in asset.commitments.filter(is_current=True):
            member_values.setdefault(commitment.member_id, Decimal("0"))
            member_values[commitment.member_id] += (
                current_value
                * Decimal(commitment.amount)
                / Decimal(asset.total_cost)
            )

        holding = _holding_summary(asset, latest_nav)
        if holding:
            holdings.append(holding)

    total_portfolio = pending_cash + rnit_value + fixed_assets + variable_assets
    allocation_values = (
        ("pending_cash", "Pending cash", pending_cash),
        ("rnit", "RNIT holdings", rnit_value),
        ("fixed_assets", "Fixed assets", fixed_assets),
        ("variable_assets", "Shares and units", variable_assets),
    )
    allocation = [
        {
            "key": key,
            "label": label,
            "value": _money(value),
            "percentage": _percentage(value, total_portfolio),
        }
        for key, label, value in allocation_values
    ]

    active_members = [member for member in members if member.is_active]
    active_value = sum(
        (member_values.get(member.id, Decimal("0")) for member in active_members),
        Decimal("0"),
    )
    top_five_value = sum(
        sorted(member_values.values(), reverse=True)[:5], Decimal("0")
    )

    segment_members = {key: [] for key, _, _ in SEGMENT_DEFINITIONS}
    for member in members:
        value = member_values.get(member.id, Decimal("0"))
        segment_members[_member_segment(member, value)].append((member, value))

    segments = []
    for key, label, range_label in SEGMENT_DEFINITIONS:
        entries = sorted(
            segment_members[key],
            key=lambda entry: (-entry[1], entry[0].full_name.lower()),
        )
        segment_value = sum((entry[1] for entry in entries), Decimal("0"))
        segments.append(
            {
                "key": key,
                "label": label,
                "range_label": range_label,
                "member_count": len(entries),
                "total_value": _money(segment_value),
                "percentage": _percentage(segment_value, total_portfolio),
                "members": [
                    {"id": member.id, "full_name": member.full_name}
                    for member, _ in entries
                ],
            }
        )

    holdings.sort(key=lambda holding: (-holding["current_value"], holding["title"]))

    return {
        "generated_on": today,
        "valuation_as_of": max(valuation_dates) if valuation_dates else None,
        "portfolio": {
            "total_value": _money(total_portfolio),
            "allocation": allocation,
        },
        "membership": {
            "total_members": len(members),
            "active_members": len(active_members),
            "inactive_members": len(members) - len(active_members),
            "average_active_portfolio": _money(
                active_value / len(active_members) if active_members else Decimal("0")
            ),
            "top_five_concentration": _percentage(top_five_value, total_portfolio),
        },
        "contributions": _contribution_summary(members, today),
        "holdings": holdings,
        "segments": segments,
    }

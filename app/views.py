from decimal import Decimal, ROUND_DOWN

from django.db import transaction
from django.db.models import Sum
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .serializers import (
    AssetCancelSerializer,
    AssetReopenSerializer,
    AssetSerializer,
    AssetWriteSerializer,
    LoginSerializer,
    LogoutSerializer,
    ContributionCreateSerializer,
    ContributionSerializer,
    ContributionVoidSerializer,
    MemberCreateSerializer,
    MemberDirectorySerializer,
    MemberSerializer,
    get_asset_funding_issues,
    allocate_commitment_reserve,
    RNITNavCreateSerializer,
    RNITNavSerializer,
    RNITPurchaseCreateSerializer,
    RNITPurchaseReverseSerializer,
    RNITPurchaseSerializer,
)
from .models import (
    Asset,
    AssetCommitment,
    Contribution,
    Member,
    RNITNav,
    RNITPurchase,
    get_active_reserved_cash,
    get_active_reserved_units,
    get_latest_rnit_nav,
    get_member_available_balance,
    get_member_available_cash,
    get_member_available_units,
    get_member_pending_cash,
    get_member_total_units,
)


class LoginView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        return Response(serializer.validated_data, status=status.HTTP_200_OK)


class MemberDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(MemberSerializer(request.user).data)


class MemberListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        members = Member.objects.order_by("full_name", "id")
        return Response(MemberDirectorySerializer(members, many=True).data)

    def post(self, request):
        if not request.user.can_manage_members:
            raise PermissionDenied("Only collective stewards can add members.")

        serializer = MemberCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        member = serializer.save()
        return Response(
            MemberDirectorySerializer(member).data,
            status=status.HTTP_201_CREATED,
        )


class ContributionPagination(PageNumberPagination):
    page_size = 20
    max_page_size = 20


class ContributionListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get_queryset(self, request):
        contributions = Contribution.objects.select_related(
            "member", "recorded_by", "voided_by"
        ).prefetch_related("rnit_purchases")
        if request.user.can_manage_members:
            return contributions
        return contributions.filter(member=request.user)

    def get(self, request):
        paginator = ContributionPagination()
        page = paginator.paginate_queryset(self.get_queryset(request), request)
        serializer = ContributionSerializer(page, many=True, context={"request": request})
        return paginator.get_paginated_response(serializer.data)

    def post(self, request):
        if not request.user.can_manage_members:
            raise PermissionDenied("Only collective stewards can record contributions.")

        serializer = ContributionCreateSerializer(
            data=request.data,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        contribution = serializer.save()
        return Response(
            ContributionSerializer(contribution, context={"request": request}).data,
            status=status.HTTP_201_CREATED,
        )


class ContributionVoidView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, contribution_id):
        if not request.user.can_manage_members:
            raise PermissionDenied("Only collective stewards can void contributions.")

        serializer = ContributionVoidSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            contribution = Contribution.objects.select_for_update().select_related(
                "member", "recorded_by", "voided_by"
            ).filter(pk=contribution_id).first()
            if contribution is None:
                return Response(status=status.HTTP_404_NOT_FOUND)
            if contribution.is_voided:
                return Response(
                    {"detail": "This contribution has already been voided."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            contribution.void(
                by_member=request.user,
                reason=serializer.validated_data["reason"],
            )
            contribution.refresh_from_db()
        return Response(ContributionSerializer(contribution, context={"request": request}).data)


def require_asset_steward(member):
    if not member.can_manage_members:
        raise PermissionDenied("Only collective stewards can manage assets.")


def visible_assets(request):
    assets = Asset.objects.prefetch_related("commitments__member")
    if request.user.can_manage_members:
        return assets
    return assets.exclude(status=Asset.Status.DRAFT)


def reserve_asset(asset):
    commitments = list(
        asset.commitments.filter(is_current=True).select_related("member").all()
    )
    commitment_member_ids = [commitment.member_id for commitment in commitments]
    list(
        Member.objects.select_for_update()
        .filter(id__in=commitment_member_ids)
        .order_by("id")
    )
    issues = get_asset_funding_issues(asset)
    if issues:
        return issues

    now = timezone.now()
    nav = get_latest_rnit_nav()
    for commitment in commitments:
        allocate_commitment_reserve(commitment, nav)
        commitment.reserved_at = now
        commitment.save(
            update_fields=(
                "reserved_cash",
                "reserved_units",
                "reservation_nav",
                "reserved_at",
                "updated_at",
            )
        )
    asset.status = Asset.Status.OPEN
    asset.save(update_fields=("status", "updated_at"))
    return []


class AssetListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(
            AssetSerializer(
                visible_assets(request),
                many=True,
                context={"request": request},
            ).data
        )

    def post(self, request):
        require_asset_steward(request.user)
        serializer = AssetWriteSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            asset = serializer.save()
            issues = reserve_asset(asset)
            asset.refresh_from_db()

        response_data = AssetSerializer(asset, context={"request": request}).data
        response_data["created_as_draft"] = bool(issues)
        return Response(response_data, status=status.HTTP_201_CREATED)


class AssetDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get_asset(self, request, asset_id):
        asset = visible_assets(request).filter(pk=asset_id).first()
        if asset is None:
            return None
        return asset

    def get(self, request, asset_id):
        asset = self.get_asset(request, asset_id)
        if asset is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return Response(AssetSerializer(asset, context={"request": request}).data)

    def patch(self, request, asset_id):
        require_asset_steward(request.user)
        with transaction.atomic():
            asset = Asset.objects.select_for_update().filter(pk=asset_id).first()
            if asset is None:
                return Response(status=status.HTTP_404_NOT_FOUND)
            if asset.status != Asset.Status.DRAFT:
                return Response(
                    {"detail": "Only private drafts can be edited."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            serializer = AssetWriteSerializer(
                asset,
                data=request.data,
                context={"request": request},
            )
            serializer.is_valid(raise_exception=True)
            asset = serializer.save()

        return Response(AssetSerializer(asset, context={"request": request}).data)


class AssetOpenView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, asset_id):
        require_asset_steward(request.user)
        with transaction.atomic():
            asset = Asset.objects.select_for_update().filter(pk=asset_id).first()
            if asset is None:
                return Response(status=status.HTTP_404_NOT_FOUND)
            if asset.status != Asset.Status.DRAFT:
                return Response(
                    {"detail": "Only private drafts can be opened."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            issues = reserve_asset(asset)
            if issues:
                return Response(
                    {
                        "detail": "This asset is not ready to open.",
                        "funding_issues": issues,
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            asset.refresh_from_db()

        return Response(AssetSerializer(asset, context={"request": request}).data)


class AssetAcquireView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, asset_id):
        require_asset_steward(request.user)
        with transaction.atomic():
            asset = Asset.objects.select_for_update().filter(pk=asset_id).first()
            if asset is None:
                return Response(status=status.HTTP_404_NOT_FOUND)
            if asset.status != Asset.Status.OPEN:
                return Response(
                    {"detail": "Only open assets can be marked acquired."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            commitments = list(
                asset.commitments.filter(is_current=True).select_for_update().all()
            )
            nav = get_latest_rnit_nav()
            reserved_value = sum(
                (
                    Decimal(commitment.reserved_cash)
                    + commitment.reserved_units
                    * (nav.nav_per_unit if nav else Decimal("0"))
                )
                for commitment in commitments
            )
            if reserved_value < Decimal(asset.total_cost):
                return Response(
                    {
                        "detail": "The live value of the reserved RNIT units is below this asset's stated cost. Reopen it to revise commitments or cancel it."
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if any(commitment.reserved_units for commitment in commitments) and nav is None:
                return Response(
                    {"detail": "Record a current RNIT NAV before acquiring this asset."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            for commitment in commitments:
                commitment.redeem(
                    by_member=request.user,
                    nav=nav.nav_per_unit if nav else None,
                )
            asset.status = Asset.Status.ACQUIRED
            asset.save(update_fields=("status", "updated_at"))

        return Response(AssetSerializer(asset, context={"request": request}).data)


class AssetCancelView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, asset_id):
        require_asset_steward(request.user)
        serializer = AssetCancelSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            asset = Asset.objects.select_for_update().filter(pk=asset_id).first()
            if asset is None:
                return Response(status=status.HTTP_404_NOT_FOUND)
            if asset.status not in (Asset.Status.DRAFT, Asset.Status.OPEN):
                return Response(
                    {"detail": "Only draft or open assets can be cancelled."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            reason = serializer.validated_data["reason"]
            for commitment in asset.commitments.filter(is_current=True).select_for_update().all():
                commitment.release(by_member=request.user, reason=reason)
            asset.status = Asset.Status.CANCELLED
            asset.cancelled_at = timezone.now()
            asset.cancelled_by = request.user
            asset.cancellation_reason = reason
            asset.full_clean()
            asset.save(
                update_fields=(
                    "status",
                    "cancelled_at",
                    "cancelled_by",
                    "cancellation_reason",
                    "updated_at",
                )
            )

        return Response(AssetSerializer(asset, context={"request": request}).data)


class AssetReopenView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, asset_id):
        require_asset_steward(request.user)
        serializer = AssetReopenSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            asset = Asset.objects.select_for_update().filter(pk=asset_id).first()
            if asset is None:
                return Response(status=status.HTTP_404_NOT_FOUND)
            if asset.status != Asset.Status.OPEN:
                return Response(
                    {"detail": "Only open assets can be reopened for revision."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            reason = serializer.validated_data["reason"]
            for commitment in asset.commitments.filter(is_current=True).select_for_update():
                commitment.release(by_member=request.user, reason=reason)
                commitment.is_current = False
                commitment.save(update_fields=("is_current", "updated_at"))
            asset.status = Asset.Status.DRAFT
            asset.reopened_at = timezone.now()
            asset.reopened_by = request.user
            asset.reopen_reason = reason
            asset.save(
                update_fields=(
                    "status",
                    "reopened_at",
                    "reopened_by",
                    "reopen_reason",
                    "updated_at",
                )
            )

        return Response(AssetSerializer(asset, context={"request": request}).data)


class AssetAvailableBalanceView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        require_asset_steward(request.user)
        members = Member.objects.filter(is_active=True).order_by("full_name", "id")
        return Response(
            [
                {
                    "id": member.id,
                    "full_name": member.full_name,
                    "available_balance": get_member_available_balance(member.id),
                }
                for member in members
            ]
        )


def savings_position(member):
    nav = get_latest_rnit_nav()
    pending_cash = get_member_pending_cash(member.id)
    units_held = get_member_total_units(member.id)
    reserved_cash = get_active_reserved_cash(member.id)
    reserved_units = get_active_reserved_units(member.id)
    unit_value = units_held * nav.nav_per_unit if nav else Decimal("0")
    reserved_value = Decimal(reserved_cash) + (
        reserved_units * nav.nav_per_unit if nav else Decimal("0")
    )
    purchases = RNITPurchase.objects.filter(member=member, reversed_at__isnull=True)
    purchased_units = purchases.aggregate(total=Sum("units"))["total"] or Decimal("0")
    invested_cash = purchases.aggregate(total=Sum("contribution__amount"))["total"] or 0
    average_cost = (
        Decimal(invested_cash) / purchased_units if purchased_units else Decimal("0")
    )
    unit_cost_basis = average_cost * units_held
    return {
        "pending_cash": float(pending_cash),
        "units_held": str(units_held),
        "available_cash": float(pending_cash - Decimal(reserved_cash)),
        "available_units": str(units_held - reserved_units),
        "reserved_cash": reserved_cash,
        "reserved_units": str(reserved_units),
        "reserved_value": float(reserved_value),
        "latest_nav": {
            "as_of_date": nav.as_of_date,
            "nav_per_unit": str(nav.nav_per_unit),
        }
        if nav
        else None,
        "rnit_value": float(unit_value),
        "current_value": float(pending_cash + unit_value),
        "available_value": float(pending_cash - Decimal(reserved_cash) + unit_value - reserved_units * nav.nav_per_unit)
        if nav
        else float(pending_cash - Decimal(reserved_cash)),
        "unrealized_growth": float(unit_value - unit_cost_basis),
    }


class SavingsSummaryView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        data = {"member": savings_position(request.user)}
        if request.user.can_manage_members:
            positions = [savings_position(member) for member in Member.objects.filter(is_active=True)]
            data["collective"] = {
                "pending_cash": sum(position["pending_cash"] for position in positions),
                "rnit_value": sum(position["rnit_value"] for position in positions),
                "current_value": sum(position["current_value"] for position in positions),
                "reserved_value": sum(position["reserved_value"] for position in positions),
                "members": len(positions),
            }
        return Response(data)


class RNITNavListCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        navs = RNITNav.objects.select_related("recorded_by").all()
        return Response(RNITNavSerializer(navs, many=True).data)

    def post(self, request):
        require_asset_steward(request.user)
        serializer = RNITNavCreateSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        nav = serializer.save()
        return Response(RNITNavSerializer(nav).data, status=status.HTTP_201_CREATED)


class RNITPendingContributionView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        require_asset_steward(request.user)
        contributions = Contribution.objects.select_related("member").prefetch_related("rnit_purchases").filter(
            voided_at__isnull=True
        )
        pending = [
            contribution
            for contribution in contributions
            if not any(purchase.is_active for purchase in contribution.rnit_purchases.all())
        ]
        return Response(ContributionSerializer(pending, many=True, context={"request": request}).data)


class RNITPurchaseCreateView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, contribution_id):
        require_asset_steward(request.user)
        serializer = RNITPurchaseCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        with transaction.atomic():
            contribution = Contribution.objects.select_for_update().select_related(
                "member"
            ).filter(pk=contribution_id, voided_at__isnull=True).first()
            if contribution is None:
                return Response(status=status.HTTP_404_NOT_FOUND)
            if contribution.rnit_purchases.filter(reversed_at__isnull=True).exists():
                return Response(
                    {"detail": "This contribution has already been purchased as RNIT units."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if get_active_reserved_cash(contribution.member_id):
                return Response(
                    {"detail": "This member has pending cash reserved for an open asset. Reopen or cancel that asset before purchasing RNIT units."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            nav = serializer.validated_data["nav"]
            units = (Decimal(contribution.amount) / nav.nav_per_unit).quantize(
                Decimal("0.00000001"), rounding=ROUND_DOWN
            )
            purchase = RNITPurchase.objects.create(
                contribution=contribution,
                member=contribution.member,
                purchased_on=serializer.validated_data["purchased_on"],
                nav_per_unit=nav.nav_per_unit,
                units=units,
                recorded_by=request.user,
            )

        return Response(RNITPurchaseSerializer(purchase).data, status=status.HTTP_201_CREATED)


class RNITPurchaseListView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        require_asset_steward(request.user)
        purchases = RNITPurchase.objects.select_related("member", "contribution").all()[:50]
        return Response(RNITPurchaseSerializer(purchases, many=True).data)


class RNITPurchaseReverseView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, purchase_id):
        require_asset_steward(request.user)
        serializer = RNITPurchaseReverseSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        with transaction.atomic():
            purchase = RNITPurchase.objects.select_for_update().filter(pk=purchase_id).first()
            if purchase is None:
                return Response(status=status.HTTP_404_NOT_FOUND)
            purchase.reverse(by_member=request.user, reason=serializer.validated_data["reason"])
        return Response(RNITPurchaseSerializer(purchase).data)


class LogoutView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        RefreshToken(serializer.validated_data["refresh"]).blacklist()
        return Response(status=status.HTTP_204_NO_CONTENT)

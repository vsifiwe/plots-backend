from django.db import transaction
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
)
from .models import Asset, AssetCommitment, Contribution, Member, get_member_available_balance


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
        )
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
    commitment_member_ids = list(asset.commitments.values_list("member_id", flat=True))
    list(
        Member.objects.select_for_update()
        .filter(id__in=commitment_member_ids)
        .order_by("id")
    )
    issues = get_asset_funding_issues(asset)
    if issues:
        return issues

    now = timezone.now()
    AssetCommitment.objects.filter(asset=asset).update(reserved_at=now)
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
            for commitment in asset.commitments.select_for_update().all():
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


class LogoutView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        RefreshToken(serializer.validated_data["refresh"]).blacklist()
        return Response(status=status.HTTP_204_NO_CONTENT)

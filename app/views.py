from django.db import transaction
from rest_framework import status
from rest_framework.exceptions import PermissionDenied
from rest_framework.pagination import PageNumberPagination
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .serializers import (
    LoginSerializer,
    LogoutSerializer,
    ContributionCreateSerializer,
    ContributionSerializer,
    ContributionVoidSerializer,
    MemberCreateSerializer,
    MemberDirectorySerializer,
    MemberSerializer,
)
from .models import Contribution, Member


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


class LogoutView(APIView):
    permission_classes = [AllowAny]

    def post(self, request):
        serializer = LogoutSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        RefreshToken(serializer.validated_data["refresh"]).blacklist()
        return Response(status=status.HTTP_204_NO_CONTENT)

from decimal import Decimal, ROUND_UP

from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.tokens import RefreshToken

from .models import (
    Asset,
    AssetCommitment,
    Contribution,
    RNITNav,
    RNITPurchase,
    get_active_reserved_cash,
    get_active_reserved_units,
    get_latest_rnit_nav,
    get_member_available_cash,
    get_member_available_balance,
    get_member_available_units,
    get_member_pending_cash,
    get_member_total_units,
    phone_validator,
)


Member = get_user_model()


class MemberSerializer(serializers.ModelSerializer):
    can_manage_members = serializers.BooleanField(read_only=True)
    role = serializers.SerializerMethodField()

    def get_role(self, member):
        return Member.Role.STEWARD if member.is_superuser else member.role

    class Meta:
        model = Member
        fields = (
            "id",
            "full_name",
            "email",
            "phone",
            "role",
            "can_manage_members",
        )


class MemberDirectorySerializer(serializers.ModelSerializer):
    role = serializers.SerializerMethodField()

    def get_role(self, member):
        return Member.Role.STEWARD if member.is_superuser else member.role

    class Meta:
        model = Member
        fields = ("id", "full_name", "role", "is_active", "date_joined")


class MemberCreateSerializer(serializers.Serializer):
    full_name = serializers.CharField(max_length=160, trim_whitespace=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    phone = serializers.CharField(max_length=10, required=False, allow_blank=True)
    password = serializers.CharField(trim_whitespace=False, write_only=True)
    password_confirmation = serializers.CharField(
        trim_whitespace=False,
        write_only=True,
    )

    def validate_full_name(self, value):
        full_name = " ".join(value.split())
        if not full_name:
            raise serializers.ValidationError("Enter the member's full name.")
        return full_name

    def validate_email(self, value):
        return value.strip().lower() if value else None

    def validate_phone(self, value):
        phone = value.strip() if value else None
        if phone:
            try:
                phone_validator(phone)
            except DjangoValidationError as error:
                raise serializers.ValidationError(error.messages) from error
        return phone

    def validate_password(self, value):
        try:
            validate_password(value)
        except DjangoValidationError as error:
            raise serializers.ValidationError(error.messages) from error
        return value

    def validate(self, attrs):
        email = attrs.get("email")
        phone = attrs.get("phone")

        if not email and not phone:
            raise serializers.ValidationError(
                {"non_field_errors": "Enter an email address or mobile number."}
            )
        if attrs["password"] != attrs["password_confirmation"]:
            raise serializers.ValidationError(
                {"password_confirmation": "Passwords do not match."}
            )
        if email and Member.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError(
                {"email": "A member already uses this email address."}
            )
        if phone and Member.objects.filter(phone=phone).exists():
            raise serializers.ValidationError(
                {"phone": "A member already uses this mobile number."}
            )

        return attrs

    def create(self, validated_data):
        password = validated_data.pop("password")
        validated_data.pop("password_confirmation")
        return Member.objects.create_user(password=password, **validated_data)


class ContributionSerializer(serializers.ModelSerializer):
    status = serializers.SerializerMethodField()
    member = serializers.SerializerMethodField()
    note = serializers.SerializerMethodField()
    void_reason = serializers.SerializerMethodField()
    recorded_by = serializers.SerializerMethodField()
    voided_by = serializers.SerializerMethodField()
    rnit_status = serializers.SerializerMethodField()

    def can_manage_contributions(self):
        request = self.context.get("request")
        return bool(request and request.user.can_manage_members)

    def get_fields(self):
        fields = super().get_fields()
        if not self.can_manage_contributions():
            for field in (
                "member",
                "note",
                "void_reason",
                "recorded_by",
                "voided_by",
            ):
                fields.pop(field)
        return fields

    def get_status(self, contribution):
        return "voided" if contribution.is_voided else "recorded"

    def get_member(self, contribution):
        return {"id": contribution.member_id, "full_name": contribution.member.full_name}

    def get_note(self, contribution):
        return contribution.note

    def get_void_reason(self, contribution):
        if not contribution.is_voided:
            return None
        return contribution.void_reason

    def get_recorded_by(self, contribution):
        return {
            "id": contribution.recorded_by_id,
            "full_name": contribution.recorded_by.full_name,
        }

    def get_voided_by(self, contribution):
        if not contribution.voided_by_id:
            return None
        return {
            "id": contribution.voided_by_id,
            "full_name": contribution.voided_by.full_name,
        }

    def get_rnit_status(self, contribution):
        purchases = contribution.rnit_purchases.all()
        if any(purchase.is_active for purchase in purchases):
            return "invested"
        return "purchase_reversed" if purchases else "pending_cash"

    class Meta:
        model = Contribution
        fields = (
            "id",
            "member",
            "amount",
            "received_on",
            "status",
            "note",
            "recorded_by",
            "created_at",
            "voided_at",
            "void_reason",
            "voided_by",
            "rnit_status",
        )


class ContributionCreateSerializer(serializers.ModelSerializer):
    member = serializers.PrimaryKeyRelatedField(
        queryset=Member.objects.filter(is_active=True)
    )

    class Meta:
        model = Contribution
        fields = ("member", "amount", "received_on", "note")
        extra_kwargs = {
            "amount": {"min_value": 1},
            "note": {"required": False, "allow_blank": True},
        }

    def validate_received_on(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError(
                "The received date cannot be in the future."
            )
        return value

    def validate_note(self, value):
        return value.strip()

    def create(self, validated_data):
        return Contribution.objects.create(
            recorded_by=self.context["request"].user,
            **validated_data,
        )


class ContributionVoidSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500, trim_whitespace=True)

    def validate_reason(self, value):
        if not value:
            raise serializers.ValidationError("Enter a reason for voiding this entry.")
        return value


class AssetCommitmentInputSerializer(serializers.Serializer):
    member = serializers.PrimaryKeyRelatedField(
        queryset=Member.objects.filter(is_active=True)
    )
    amount = serializers.IntegerField(min_value=1)


def current_asset_commitments(asset):
    return asset.commitments.filter(is_current=True).select_related("member")


def get_asset_funding_issues(asset):
    commitments = list(current_asset_commitments(asset))
    committed_total = sum(commitment.amount for commitment in commitments)
    issues = []

    if committed_total != asset.total_cost:
        issues.append(
            {
                "type": "cost_mismatch",
                "message": "Commitments must total exactly to the asset cost.",
                "committed_total": committed_total,
                "total_cost": asset.total_cost,
            }
        )

    nav = get_latest_rnit_nav()
    for commitment in commitments:
        available_cash = get_member_available_cash(commitment.member_id)
        available_units = get_member_available_units(commitment.member_id)
        available_balance = available_cash + (
            available_units * nav.nav_per_unit if nav else Decimal("0")
        )
        if commitment.amount > available_balance:
            issues.append(
                {
                    "type": "insufficient_balance",
                    "member": {
                        "id": commitment.member_id,
                        "full_name": commitment.member.full_name,
                    },
                    "required": commitment.amount,
                    "available_balance": available_balance,
                }
            )
        elif commitment.amount > available_cash and nav is None:
            issues.append(
                {
                    "type": "nav_required",
                    "message": "Record a current RNIT NAV before reserving invested funds.",
                }
            )

    return issues


def allocate_commitment_reserve(commitment, nav):
    available_cash = get_member_available_cash(commitment.member_id)
    reserved_cash = min(Decimal(commitment.amount), available_cash)
    remaining = Decimal(commitment.amount) - reserved_cash
    reserved_units = Decimal("0")
    reservation_nav = None
    if remaining:
        if nav is None:
            raise serializers.ValidationError("A current RNIT NAV is required.")
        reserved_units = (remaining / nav.nav_per_unit).quantize(
            Decimal("0.00000001"), rounding=ROUND_UP
        )
        if reserved_units > get_member_available_units(commitment.member_id):
            raise serializers.ValidationError("The member no longer has enough available RNIT units.")
        reservation_nav = nav.nav_per_unit

    commitment.reserved_cash = int(reserved_cash)
    commitment.reserved_units = reserved_units
    commitment.reservation_nav = reservation_nav


class AssetWriteSerializer(serializers.Serializer):
    title = serializers.CharField(max_length=160, trim_whitespace=True)
    description = serializers.CharField(
        max_length=2000,
        required=False,
        allow_blank=True,
        trim_whitespace=True,
    )
    total_cost = serializers.IntegerField(min_value=1)
    commitments = AssetCommitmentInputSerializer(many=True)

    def validate_title(self, value):
        if not value:
            raise serializers.ValidationError("Enter an asset title.")
        return value

    def validate(self, attrs):
        member_ids = [commitment["member"].id for commitment in attrs["commitments"]]
        if len(member_ids) != len(set(member_ids)):
            raise serializers.ValidationError(
                {"commitments": "Add each member only once."}
            )
        return attrs

    def create(self, validated_data):
        commitments = validated_data.pop("commitments")
        asset = Asset.objects.create(
            created_by=self.context["request"].user,
            **validated_data,
        )
        AssetCommitment.objects.bulk_create(
            [
                AssetCommitment(
                    asset=asset,
                    member=commitment["member"],
                    amount=commitment["amount"],
                )
                for commitment in commitments
            ]
        )
        return asset

    def update(self, asset, validated_data):
        commitments = validated_data.pop("commitments")
        asset.title = validated_data["title"]
        asset.description = validated_data.get("description", "")
        asset.total_cost = validated_data["total_cost"]
        asset.save(update_fields=("title", "description", "total_cost", "updated_at"))

        asset.commitments.filter(is_current=True).update(is_current=False)
        AssetCommitment.objects.bulk_create(
            [
                AssetCommitment(
                    asset=asset,
                    member=commitment["member"],
                    amount=commitment["amount"],
                )
                for commitment in commitments
            ]
        )
        return asset


class AssetCommitmentSerializer(serializers.ModelSerializer):
    member = serializers.SerializerMethodField()
    ownership_percentage = serializers.SerializerMethodField()
    is_reserved = serializers.BooleanField(read_only=True)
    live_reserved_value = serializers.SerializerMethodField()

    def get_member(self, commitment):
        return {
            "id": commitment.member_id,
            "full_name": commitment.member.full_name,
        }

    def get_ownership_percentage(self, commitment):
        return round((commitment.amount / commitment.asset.total_cost) * 100, 2)

    def get_live_reserved_value(self, commitment):
        nav = get_latest_rnit_nav()
        return commitment.reserved_cash + float(
            commitment.reserved_units * (nav.nav_per_unit if nav else Decimal("0"))
        )

    class Meta:
        model = AssetCommitment
        fields = (
            "id",
            "member",
            "amount",
            "ownership_percentage",
            "is_reserved",
            "created_at",
            "reserved_at",
            "released_at",
            "release_reason",
            "reserved_cash",
            "reserved_units",
            "reservation_nav",
            "redeemed_at",
            "redemption_nav",
            "live_reserved_value",
        )


class AssetSerializer(serializers.ModelSerializer):
    total_committed = serializers.SerializerMethodField()
    total_reserved = serializers.SerializerMethodField()
    your_commitment = serializers.SerializerMethodField()
    commitments = serializers.SerializerMethodField()
    funding_issues = serializers.SerializerMethodField()

    def can_manage_assets(self):
        request = self.context.get("request")
        return bool(request and request.user.can_manage_members)

    def get_fields(self):
        fields = super().get_fields()
        if not self.can_manage_assets():
            fields.pop("commitments")
            fields.pop("funding_issues")
            fields.pop("cancellation_reason")
        return fields

    def get_commitments_for_asset(self, asset):
        return list(
            asset.commitments.filter(is_current=True).select_related("member", "asset")
        )

    def get_total_committed(self, asset):
        return sum(commitment.amount for commitment in self.get_commitments_for_asset(asset))

    def get_total_reserved(self, asset):
        nav = get_latest_rnit_nav()
        return sum(
            float(
                Decimal(commitment.reserved_cash)
                + commitment.reserved_units
                * (nav.nav_per_unit if nav else Decimal("0"))
            )
            for commitment in self.get_commitments_for_asset(asset)
            if commitment.is_reserved
        )

    def get_your_commitment(self, asset):
        request = self.context.get("request")
        if not request:
            return None
        commitment = asset.commitments.filter(
            is_current=True, member=request.user
        ).select_related("member", "asset").first()
        if commitment is None:
            return None
        data = AssetCommitmentSerializer(commitment).data
        data.pop("release_reason")
        return data

    def get_commitments(self, asset):
        return AssetCommitmentSerializer(
            self.get_commitments_for_asset(asset), many=True
        ).data

    def get_funding_issues(self, asset):
        if asset.status != Asset.Status.DRAFT:
            return []
        return get_asset_funding_issues(asset)

    class Meta:
        model = Asset
        fields = (
            "id",
            "title",
            "description",
            "total_cost",
            "status",
            "total_committed",
            "total_reserved",
            "your_commitment",
            "commitments",
            "funding_issues",
            "created_at",
            "updated_at",
            "cancelled_at",
            "cancellation_reason",
        )


class AssetCancelSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500, trim_whitespace=True)

    def validate_reason(self, value):
        if not value:
            raise serializers.ValidationError("Enter a reason for cancelling this asset.")
        return value


class AssetReopenSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500, trim_whitespace=True)

    def validate_reason(self, value):
        if not value:
            raise serializers.ValidationError("Enter a reason for reopening this asset.")
        return value


class RNITNavSerializer(serializers.ModelSerializer):
    recorded_by = serializers.SerializerMethodField()

    def get_recorded_by(self, nav):
        return {"id": nav.recorded_by_id, "full_name": nav.recorded_by.full_name}

    class Meta:
        model = RNITNav
        fields = ("id", "as_of_date", "nav_per_unit", "recorded_by", "created_at")


class RNITNavCreateSerializer(serializers.ModelSerializer):
    class Meta:
        model = RNITNav
        fields = ("as_of_date", "nav_per_unit")

    def validate_as_of_date(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("The NAV date cannot be in the future.")
        return value

    def create(self, validated_data):
        return RNITNav.objects.create(
            recorded_by=self.context["request"].user,
            **validated_data,
        )


class RNITPurchaseSerializer(serializers.ModelSerializer):
    member = serializers.SerializerMethodField()
    contribution_id = serializers.IntegerField(read_only=True)
    status = serializers.SerializerMethodField()

    def get_member(self, purchase):
        return {"id": purchase.member_id, "full_name": purchase.member.full_name}

    def get_status(self, purchase):
        return "reversed" if not purchase.is_active else "invested"

    class Meta:
        model = RNITPurchase
        fields = (
            "id",
            "contribution_id",
            "member",
            "purchased_on",
            "nav_per_unit",
            "units",
            "status",
            "created_at",
            "reversed_at",
            "reversal_reason",
        )


class RNITPurchaseCreateSerializer(serializers.Serializer):
    purchased_on = serializers.DateField()
    nav_id = serializers.PrimaryKeyRelatedField(
        source="nav", queryset=RNITNav.objects.all()
    )

    def validate_purchased_on(self, value):
        if value > timezone.localdate():
            raise serializers.ValidationError("The purchase date cannot be in the future.")
        return value

    def validate(self, attrs):
        if attrs["nav"].as_of_date > attrs["purchased_on"]:
            raise serializers.ValidationError(
                {"nav_id": "Choose a NAV dated on or before the purchase date."}
            )
        return attrs


class RNITPurchaseReverseSerializer(serializers.Serializer):
    reason = serializers.CharField(max_length=500, trim_whitespace=True)

    def validate_reason(self, value):
        if not value:
            raise serializers.ValidationError("Enter a reason for reversing this purchase.")
        return value


class LoginSerializer(serializers.Serializer):
    identifier = serializers.CharField(max_length=254, trim_whitespace=True)
    password = serializers.CharField(trim_whitespace=False, write_only=True)

    default_error_messages = {
        "invalid_credentials": "Unable to sign in with those details.",
        "invalid_identifier": "Enter a valid email address or mobile number.",
    }

    def validate(self, attrs):
        identifier = attrs["identifier"].strip()
        password = attrs["password"]

        if not identifier or not password:
            self.fail("invalid_credentials")

        if "@" in identifier:
            try:
                validate_email(identifier)
            except DjangoValidationError:
                self.fail("invalid_identifier")
            member = Member.objects.filter(email__iexact=identifier).first()
        else:
            try:
                phone_validator(identifier)
            except DjangoValidationError:
                self.fail("invalid_identifier")
            member = Member.objects.filter(phone=identifier).first()

        if member is None:
            make_password(password)
            self.fail("invalid_credentials")

        if not member.is_active or not member.check_password(password):
            self.fail("invalid_credentials")

        refresh = RefreshToken.for_user(member)
        return {
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "user": MemberSerializer(member).data,
        }


class LogoutSerializer(serializers.Serializer):
    refresh = serializers.CharField()

    def validate_refresh(self, value):
        try:
            RefreshToken(value)
        except Exception as error:
            raise serializers.ValidationError("Invalid refresh token.") from error
        return value

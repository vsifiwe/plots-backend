from django.contrib.auth import get_user_model
from django.contrib.auth.hashers import make_password
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from django.core.validators import validate_email
from django.utils import timezone
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.tokens import RefreshToken

from .models import Contribution, phone_validator


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

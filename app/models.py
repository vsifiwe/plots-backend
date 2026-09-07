from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from django.db.models import Q, Sum
from django.db.models.functions import Lower
from django.utils import timezone


phone_validator = RegexValidator(
    regex=r"^(078|079|072|073)\d{7}$",
    message="Enter a 10-digit mobile number beginning with 078, 079, 072, or 073.",
)


class MemberManager(BaseUserManager):
    def _create_user(self, email=None, phone=None, password=None, **extra_fields):
        email = self.normalize_email(email).lower() if email else None
        phone = phone.strip() if phone else None

        if not email and not phone:
            raise ValueError("A member must have an email address or phone number.")
        if password is None:
            raise ValueError("Members must have a password.")

        member = self.model(email=email, phone=phone, **extra_fields)
        member.full_clean(exclude=["password"])
        member.set_password(password)
        member.save(using=self._db)
        return member

    def create_user(self, email=None, phone=None, password=None, **extra_fields):
        extra_fields.setdefault("is_staff", False)
        extra_fields.setdefault("is_superuser", False)
        return self._create_user(email, phone, password, **extra_fields)

    def create_superuser(self, email, password, **extra_fields):
        extra_fields.setdefault("is_staff", True)
        extra_fields.setdefault("is_superuser", True)
        extra_fields.setdefault("is_active", True)
        extra_fields.setdefault("role", "steward")

        if not extra_fields.get("is_staff"):
            raise ValueError("Superusers must have is_staff=True.")
        if not extra_fields.get("is_superuser"):
            raise ValueError("Superusers must have is_superuser=True.")
        if not extra_fields.get("full_name"):
            raise ValueError("Superusers must have a full name.")

        return self._create_user(email=email, password=password, **extra_fields)


class Member(AbstractBaseUser, PermissionsMixin):
    class Role(models.TextChoices):
        MEMBER = "member", "Member"
        STEWARD = "steward", "Steward"

    full_name = models.CharField(max_length=160)
    email = models.EmailField(blank=True, null=True, unique=True)
    phone = models.CharField(
        blank=True,
        max_length=10,
        null=True,
        unique=True,
        validators=[phone_validator],
    )
    is_active = models.BooleanField(default=True)
    is_staff = models.BooleanField(default=False)
    role = models.CharField(
        choices=Role.choices,
        default=Role.MEMBER,
        max_length=16,
    )
    date_joined = models.DateTimeField(auto_now_add=True)

    objects = MemberManager()

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["full_name"]

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=Q(email__isnull=False) | Q(phone__isnull=False),
                name="member_requires_email_or_phone",
            ),
            models.UniqueConstraint(
                Lower("email"),
                condition=Q(email__isnull=False),
                name="member_email_ci_unique",
            ),
        ]

    def clean(self):
        super().clean()
        self.full_name = " ".join(self.full_name.split()) if self.full_name else ""
        self.email = self.email.strip().lower() if self.email else None
        self.phone = self.phone.strip() if self.phone else None

        if not self.full_name:
            raise ValidationError({"full_name": "Enter the member's full name."})
        if not self.email and not self.phone:
            raise ValidationError("A member must have an email address or phone number.")

    @property
    def can_manage_members(self):
        return self.is_superuser or self.role == self.Role.STEWARD

    def __str__(self):
        return self.full_name


class Contribution(models.Model):
    member = models.ForeignKey(
        Member,
        on_delete=models.PROTECT,
        related_name="contributions",
    )
    amount = models.PositiveBigIntegerField()
    received_on = models.DateField(db_index=True)
    note = models.TextField(blank=True, max_length=500)
    recorded_by = models.ForeignKey(
        Member,
        on_delete=models.PROTECT,
        related_name="recorded_contributions",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    voided_at = models.DateTimeField(blank=True, null=True)
    voided_by = models.ForeignKey(
        Member,
        blank=True,
        null=True,
        on_delete=models.PROTECT,
        related_name="voided_contributions",
    )
    void_reason = models.TextField(blank=True, max_length=500)

    class Meta:
        ordering = ("-received_on", "-created_at", "-id")
        constraints = [
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="contribution_amount_is_positive",
            ),
            models.CheckConstraint(
                condition=(
                    Q(voided_at__isnull=True, voided_by__isnull=True, void_reason="")
                    | (
                        Q(voided_at__isnull=False, voided_by__isnull=False)
                        & ~Q(void_reason="")
                    )
                ),
                name="contribution_void_requires_audit",
            ),
        ]

    @property
    def is_voided(self):
        return self.voided_at is not None

    def void(self, *, by_member, reason):
        if self.is_voided:
            raise ValidationError("This contribution has already been voided.")

        remaining_contributions = (
            Contribution.objects.filter(member=self.member, voided_at__isnull=True)
            .exclude(pk=self.pk)
            .aggregate(total=Sum("amount"))["total"]
            or 0
        )
        if remaining_contributions < get_active_commitment_total(self.member_id):
            raise ValidationError(
                "This contribution cannot be voided because funds are reserved for an active asset."
            )

        self.voided_at = timezone.now()
        self.voided_by = by_member
        self.void_reason = reason
        self.full_clean()
        self.save(update_fields=("voided_at", "voided_by", "void_reason"))

    def __str__(self):
        return f"{self.member.full_name} · {self.amount} RWF"


class Asset(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        OPEN = "open", "Open"
        ACQUIRED = "acquired", "Acquired"
        CANCELLED = "cancelled", "Cancelled"

    title = models.CharField(max_length=160)
    description = models.TextField(blank=True, max_length=2000)
    total_cost = models.PositiveBigIntegerField()
    status = models.CharField(
        choices=Status.choices,
        default=Status.DRAFT,
        max_length=16,
    )
    created_by = models.ForeignKey(
        Member,
        on_delete=models.PROTECT,
        related_name="created_assets",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    cancelled_at = models.DateTimeField(blank=True, null=True)
    cancelled_by = models.ForeignKey(
        Member,
        blank=True,
        null=True,
        on_delete=models.PROTECT,
        related_name="cancelled_assets",
    )
    cancellation_reason = models.TextField(blank=True, max_length=500)

    class Meta:
        ordering = ("-created_at", "-id")
        constraints = [
            models.CheckConstraint(
                condition=Q(total_cost__gt=0),
                name="asset_total_cost_is_positive",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        status="cancelled",
                        cancelled_at__isnull=False,
                        cancelled_by__isnull=False,
                    )
                    & ~Q(cancellation_reason="")
                    | Q(
                        ~Q(status="cancelled"),
                        cancelled_at__isnull=True,
                        cancelled_by__isnull=True,
                        cancellation_reason="",
                    )
                ),
                name="asset_cancellation_requires_audit",
            ),
        ]

    def __str__(self):
        return self.title


class AssetCommitment(models.Model):
    asset = models.ForeignKey(
        Asset,
        on_delete=models.PROTECT,
        related_name="commitments",
    )
    member = models.ForeignKey(
        Member,
        on_delete=models.PROTECT,
        related_name="asset_commitments",
    )
    amount = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    reserved_at = models.DateTimeField(blank=True, null=True)
    released_at = models.DateTimeField(blank=True, null=True)
    released_by = models.ForeignKey(
        Member,
        blank=True,
        null=True,
        on_delete=models.PROTECT,
        related_name="released_asset_commitments",
    )
    release_reason = models.TextField(blank=True, max_length=500)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("asset", "member"),
                name="asset_one_commitment_per_member",
            ),
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="asset_commitment_amount_is_positive",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        released_at__isnull=True,
                        released_by__isnull=True,
                        release_reason="",
                    )
                    | (
                        Q(released_at__isnull=False, released_by__isnull=False)
                        & ~Q(release_reason="")
                    )
                ),
                name="asset_commitment_release_requires_audit",
            ),
        ]

    @property
    def is_reserved(self):
        return self.reserved_at is not None and self.released_at is None

    def release(self, *, by_member, reason):
        if not self.is_reserved:
            return

        self.released_at = timezone.now()
        self.released_by = by_member
        self.release_reason = reason
        self.full_clean()
        self.save(
            update_fields=("released_at", "released_by", "release_reason", "updated_at")
        )

    def __str__(self):
        return f"{self.asset.title} · {self.member.full_name} · {self.amount} RWF"


def get_active_commitment_total(member_id):
    return (
        AssetCommitment.objects.filter(
            member_id=member_id,
            asset__status__in=(Asset.Status.OPEN, Asset.Status.ACQUIRED),
            reserved_at__isnull=False,
            released_at__isnull=True,
        ).aggregate(total=Sum("amount"))["total"]
        or 0
    )


def get_member_available_balance(member_id):
    contributions = (
        Contribution.objects.filter(member_id=member_id, voided_at__isnull=True)
        .aggregate(total=Sum("amount"))["total"]
        or 0
    )
    return contributions - get_active_commitment_total(member_id)

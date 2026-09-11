from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
from django.contrib.auth.models import PermissionsMixin
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models
from decimal import Decimal

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

        if self.rnit_purchases.filter(reversed_at__isnull=True).exists():
            raise ValidationError(
                "This contribution has been converted into RNIT units and must be corrected by reversing that purchase."
            )

        remaining_cash = get_member_pending_cash(self.member_id, exclude_id=self.pk)
        if remaining_cash < get_active_reserved_cash(self.member_id):
            raise ValidationError(
                "This contribution cannot be voided because pending cash is reserved for an active asset."
            )

        self.voided_at = timezone.now()
        self.voided_by = by_member
        self.void_reason = reason
        self.full_clean()
        self.save(update_fields=("voided_at", "voided_by", "void_reason"))

    def __str__(self):
        return f"{self.member.full_name} · {self.amount} RWF"


class RNITNav(models.Model):
    as_of_date = models.DateField(unique=True, db_index=True)
    nav_per_unit = models.DecimalField(max_digits=20, decimal_places=8)
    recorded_by = models.ForeignKey(
        Member,
        on_delete=models.PROTECT,
        related_name="recorded_rnit_navs",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-as_of_date", "-id")
        constraints = [
            models.CheckConstraint(
                condition=Q(nav_per_unit__gt=0),
                name="rnit_nav_is_positive",
            )
        ]

    def __str__(self):
        return f"RNIT NAV · {self.as_of_date} · {self.nav_per_unit}"


class RNITPurchase(models.Model):
    contribution = models.ForeignKey(
        Contribution,
        on_delete=models.PROTECT,
        related_name="rnit_purchases",
    )
    member = models.ForeignKey(
        Member,
        on_delete=models.PROTECT,
        related_name="rnit_purchases",
    )
    purchased_on = models.DateField(db_index=True)
    nav_per_unit = models.DecimalField(max_digits=20, decimal_places=8)
    units = models.DecimalField(max_digits=24, decimal_places=8)
    recorded_by = models.ForeignKey(
        Member,
        on_delete=models.PROTECT,
        related_name="recorded_rnit_purchases",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    reversed_at = models.DateTimeField(blank=True, null=True)
    reversed_by = models.ForeignKey(
        Member,
        blank=True,
        null=True,
        on_delete=models.PROTECT,
        related_name="reversed_rnit_purchases",
    )
    reversal_reason = models.TextField(blank=True, max_length=500)

    class Meta:
        ordering = ("-purchased_on", "-created_at", "-id")
        constraints = [
            models.CheckConstraint(
                condition=Q(nav_per_unit__gt=0),
                name="rnit_purchase_nav_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(units__gt=0),
                name="rnit_purchase_units_are_positive",
            ),
            models.UniqueConstraint(
                fields=("contribution",),
                condition=Q(reversed_at__isnull=True),
                name="rnit_one_active_purchase_per_contribution",
            ),
            models.CheckConstraint(
                condition=(
                    Q(
                        reversed_at__isnull=True,
                        reversed_by__isnull=True,
                        reversal_reason="",
                    )
                    | (
                        Q(reversed_at__isnull=False, reversed_by__isnull=False)
                        & ~Q(reversal_reason="")
                    )
                ),
                name="rnit_purchase_reversal_requires_audit",
            ),
        ]

    @property
    def is_active(self):
        return self.reversed_at is None

    def reverse(self, *, by_member, reason):
        if not self.is_active:
            raise ValidationError("This RNIT purchase has already been reversed.")
        if get_member_available_units(self.member_id) < self.units:
            raise ValidationError(
                "This RNIT purchase cannot be reversed because some of its units are reserved or have funded an acquired asset."
            )

        self.reversed_at = timezone.now()
        self.reversed_by = by_member
        self.reversal_reason = reason
        self.full_clean()
        self.save(update_fields=("reversed_at", "reversed_by", "reversal_reason"))

    def __str__(self):
        return f"{self.member.full_name} · {self.units} RNIT units"


class Asset(models.Model):
    class AssetType(models.TextChoices):
        FIXED = "fixed", "Fixed"
        VARIABLE = "variable", "Variable"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        OPEN = "open", "Open"
        ACQUIRED = "acquired", "Acquired"
        CANCELLED = "cancelled", "Cancelled"

    title = models.CharField(max_length=160)
    description = models.TextField(blank=True, max_length=2000)
    total_cost = models.PositiveBigIntegerField()
    asset_type = models.CharField(
        choices=AssetType.choices,
        default=AssetType.FIXED,
        max_length=16,
    )
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
    acquired_at = models.DateTimeField(blank=True, null=True)
    acquired_units = models.DecimalField(
        blank=True,
        null=True,
        max_digits=24,
        decimal_places=8,
    )
    acquisition_unit_price = models.DecimalField(
        blank=True,
        null=True,
        max_digits=20,
        decimal_places=8,
    )
    cancelled_at = models.DateTimeField(blank=True, null=True)
    cancelled_by = models.ForeignKey(
        Member,
        blank=True,
        null=True,
        on_delete=models.PROTECT,
        related_name="cancelled_assets",
    )
    cancellation_reason = models.TextField(blank=True, max_length=500)
    reopened_at = models.DateTimeField(blank=True, null=True)
    reopened_by = models.ForeignKey(
        Member,
        blank=True,
        null=True,
        on_delete=models.PROTECT,
        related_name="reopened_assets",
    )
    reopen_reason = models.TextField(blank=True, max_length=500)

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

    def clean(self):
        super().clean()
        errors = {}
        if self.asset_type == self.AssetType.FIXED:
            if self.acquired_units is not None or self.acquisition_unit_price is not None:
                errors["asset_type"] = "Fixed assets cannot have unit purchase details."
        elif self.status == self.Status.ACQUIRED:
            if self.acquired_units is None:
                errors["acquired_units"] = "Enter the units acquired for a variable asset."
            if self.acquisition_unit_price is None:
                errors["acquisition_unit_price"] = "Enter the initial unit price for a variable asset."
            if (
                self.acquired_units is not None
                and self.acquisition_unit_price is not None
                and self.acquired_units * self.acquisition_unit_price
                != Decimal(self.total_cost)
            ):
                errors["acquisition_unit_price"] = (
                    "Units multiplied by the initial price must equal the asset cost."
                )
        if errors:
            raise ValidationError(errors)


class AssetValuation(models.Model):
    asset = models.ForeignKey(
        Asset,
        on_delete=models.PROTECT,
        related_name="valuations",
    )
    as_of_date = models.DateField(db_index=True)
    total_value = models.DecimalField(
        blank=True,
        null=True,
        max_digits=24,
        decimal_places=8,
    )
    unit_price = models.DecimalField(
        blank=True,
        null=True,
        max_digits=20,
        decimal_places=8,
    )
    recorded_by = models.ForeignKey(
        Member,
        on_delete=models.PROTECT,
        related_name="recorded_asset_valuations",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-as_of_date", "-id")
        constraints = [
            models.UniqueConstraint(
                fields=("asset", "as_of_date"),
                name="asset_one_valuation_per_effective_date",
            ),
            models.CheckConstraint(
                condition=(
                    Q(total_value__isnull=False, unit_price__isnull=True)
                    | Q(total_value__isnull=True, unit_price__isnull=False)
                ),
                name="asset_valuation_has_one_value",
            ),
            models.CheckConstraint(
                condition=Q(total_value__isnull=True) | Q(total_value__gt=0),
                name="asset_valuation_total_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(unit_price__isnull=True) | Q(unit_price__gt=0),
                name="asset_valuation_unit_price_is_positive",
            ),
        ]

    def clean(self):
        super().clean()
        errors = {}
        if self.asset_id:
            if self.asset.status != Asset.Status.ACQUIRED:
                errors["asset"] = "Only acquired assets can receive valuations."
            if self.asset.acquired_at and self.as_of_date < self.asset.acquired_at.date():
                errors["as_of_date"] = "A valuation cannot predate acquisition."
            if self.asset.asset_type == Asset.AssetType.FIXED:
                if self.total_value is None or self.unit_price is not None:
                    errors["total_value"] = "Fixed assets require one total valuation in RWF."
            elif self.unit_price is None or self.total_value is not None:
                errors["unit_price"] = "Variable assets require one unit price."
        if errors:
            raise ValidationError(errors)

    def __str__(self):
        return f"{self.asset.title} · {self.as_of_date}"


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
    is_current = models.BooleanField(default=True)
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
    reserved_cash = models.PositiveBigIntegerField(default=0)
    reserved_units = models.DecimalField(
        default=Decimal("0"), max_digits=24, decimal_places=8
    )
    reservation_nav = models.DecimalField(
        blank=True,
        null=True,
        max_digits=20,
        decimal_places=8,
    )
    redeemed_at = models.DateTimeField(blank=True, null=True)
    redeemed_by = models.ForeignKey(
        Member,
        blank=True,
        null=True,
        on_delete=models.PROTECT,
        related_name="redeemed_asset_commitments",
    )
    redemption_nav = models.DecimalField(
        blank=True,
        null=True,
        max_digits=20,
        decimal_places=8,
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=("asset", "member"),
                condition=Q(is_current=True),
                name="asset_one_commitment_per_member",
            ),
            models.CheckConstraint(
                condition=Q(amount__gt=0),
                name="asset_commitment_amount_is_positive",
            ),
            models.CheckConstraint(
                condition=Q(reserved_units__gte=0),
                name="asset_commitment_reserved_units_are_not_negative",
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
        return (
            self.reserved_at is not None
            and self.released_at is None
            and self.redeemed_at is None
        )

    def reserved_value(self, nav):
        return Decimal(self.reserved_cash) + (self.reserved_units * nav)

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

    def redeem(self, *, by_member, nav):
        if not self.is_reserved:
            raise ValidationError("This asset commitment is not available to redeem.")

        self.redeemed_at = timezone.now()
        self.redeemed_by = by_member
        self.redemption_nav = nav
        self.full_clean()
        self.save(
            update_fields=("redeemed_at", "redeemed_by", "redemption_nav", "updated_at")
        )

    def __str__(self):
        return f"{self.asset.title} · {self.member.full_name} · {self.amount} RWF"


def get_active_commitment_total(member_id):
    return (
        AssetCommitment.objects.filter(
            member_id=member_id,
            asset__status=Asset.Status.OPEN,
            is_current=True,
            reserved_at__isnull=False,
            released_at__isnull=True,
            redeemed_at__isnull=True,
        ).aggregate(total=Sum("amount"))["total"]
        or 0
    )


def get_latest_rnit_nav():
    return RNITNav.objects.order_by("-as_of_date", "-id").first()


def get_member_pending_cash(member_id, *, exclude_id=None):
    contributions = Contribution.objects.filter(member_id=member_id, voided_at__isnull=True)
    if exclude_id is not None:
        contributions = contributions.exclude(pk=exclude_id)

    total = Decimal("0")
    for contribution in contributions.prefetch_related("rnit_purchases"):
        has_active_purchase = any(
            purchase.is_active for purchase in contribution.rnit_purchases.all()
        )
        if not has_active_purchase:
            total += Decimal(contribution.amount)

    redeemed_cash = (
        AssetCommitment.objects.filter(
            member_id=member_id,
            redeemed_at__isnull=False,
        ).aggregate(total=Sum("reserved_cash"))["total"]
        or 0
    )
    return total - Decimal(redeemed_cash)


def get_member_total_units(member_id):
    purchased_units = (
        RNITPurchase.objects.filter(member_id=member_id, reversed_at__isnull=True)
        .aggregate(total=Sum("units"))["total"]
        or Decimal("0")
    )
    redeemed_units = (
        AssetCommitment.objects.filter(
            member_id=member_id,
            redeemed_at__isnull=False,
        ).aggregate(total=Sum("reserved_units"))["total"]
        or Decimal("0")
    )
    return purchased_units - redeemed_units


def get_active_reserved_cash(member_id):
    return (
        AssetCommitment.objects.filter(
            member_id=member_id,
            asset__status=Asset.Status.OPEN,
            is_current=True,
            reserved_at__isnull=False,
            released_at__isnull=True,
            redeemed_at__isnull=True,
        ).aggregate(total=Sum("reserved_cash"))["total"]
        or 0
    )


def get_active_reserved_units(member_id):
    return (
        AssetCommitment.objects.filter(
            member_id=member_id,
            asset__status=Asset.Status.OPEN,
            is_current=True,
            reserved_at__isnull=False,
            released_at__isnull=True,
            redeemed_at__isnull=True,
        ).aggregate(total=Sum("reserved_units"))["total"]
        or Decimal("0")
    )


def get_member_available_cash(member_id):
    return get_member_pending_cash(member_id) - Decimal(get_active_reserved_cash(member_id))


def get_member_available_units(member_id):
    return get_member_total_units(member_id) - get_active_reserved_units(member_id)


def get_member_available_balance(member_id):
    nav = get_latest_rnit_nav()
    available = get_member_available_cash(member_id)
    if nav:
        available += get_member_available_units(member_id) * nav.nav_per_unit
    return available

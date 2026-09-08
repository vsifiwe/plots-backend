from django import forms
from django.contrib import admin
from django.contrib.auth.forms import ReadOnlyPasswordHashField

from .models import Asset, AssetCommitment, AssetValuation, Contribution, Member, RNITNav, RNITPurchase


class MemberCreationForm(forms.ModelForm):
    password1 = forms.CharField(label="Password", strip=False, widget=forms.PasswordInput)
    password2 = forms.CharField(
        label="Confirm password", strip=False, widget=forms.PasswordInput
    )

    class Meta:
        model = Member
        fields = ("full_name", "email", "phone", "role")

    def clean_password2(self):
        password1 = self.cleaned_data.get("password1")
        password2 = self.cleaned_data.get("password2")

        if password1 and password2 and password1 != password2:
            raise forms.ValidationError("Passwords do not match.")
        return password2

    def save(self, commit=True):
        member = super().save(commit=False)
        member.set_password(self.cleaned_data["password1"])
        if commit:
            member.save()
        return member


class MemberChangeForm(forms.ModelForm):
    password = ReadOnlyPasswordHashField(label="Password")

    class Meta:
        model = Member
        fields = "__all__"


@admin.register(Member)
class MemberAdmin(admin.ModelAdmin):
    add_form = MemberCreationForm
    form = MemberChangeForm
    list_display = (
        "full_name",
        "email",
        "phone",
        "role",
        "is_active",
        "is_staff",
        "date_joined",
    )
    list_filter = ("role", "is_active", "is_staff", "is_superuser")
    search_fields = ("full_name", "email", "phone")
    ordering = ("full_name", "email", "phone")
    fieldsets = (
        (None, {"fields": ("full_name", "email", "phone", "password")} ),
        ("Member role", {"fields": ("role",)}),
        ("Permissions", {"fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")} ),
        ("Important dates", {"fields": ("last_login", "date_joined")} ),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("full_name", "email", "phone", "role", "password1", "password2"),
            },
        ),
    )

    def get_form(self, request, obj=None, **kwargs):
        if obj is None:
            kwargs["form"] = self.add_form
        return super().get_form(request, obj, **kwargs)


@admin.register(Contribution)
class ContributionAdmin(admin.ModelAdmin):
    list_display = (
        "member",
        "amount",
        "received_on",
        "is_voided",
        "recorded_by",
        "voided_at",
    )
    list_filter = ("received_on", "voided_at")
    search_fields = ("member__full_name", "recorded_by__full_name", "note", "void_reason")
    date_hierarchy = "received_on"
    ordering = ("-received_on", "-created_at", "-id")
    readonly_fields = (
        "member",
        "amount",
        "received_on",
        "note",
        "recorded_by",
        "created_at",
        "voided_at",
        "voided_by",
        "void_reason",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(RNITNav)
class RNITNavAdmin(admin.ModelAdmin):
    list_display = ("as_of_date", "nav_per_unit", "recorded_by", "created_at")
    list_filter = ("as_of_date",)
    search_fields = ("recorded_by__full_name",)
    readonly_fields = ("recorded_by", "created_at")

    def save_model(self, request, obj, form, change):
        if not change and not obj.recorded_by_id:
            obj.recorded_by = request.user
        super().save_model(request, obj, form, change)

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(RNITPurchase)
class RNITPurchaseAdmin(admin.ModelAdmin):
    list_display = ("member", "contribution", "purchased_on", "nav_per_unit", "units", "is_active")
    list_filter = ("purchased_on", "reversed_at")
    search_fields = ("member__full_name", "contribution__id")
    readonly_fields = (
        "contribution",
        "member",
        "purchased_on",
        "nav_per_unit",
        "units",
        "recorded_by",
        "created_at",
        "reversed_at",
        "reversed_by",
        "reversal_reason",
    )

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


class AssetCommitmentInline(admin.TabularInline):
    model = AssetCommitment
    extra = 0
    autocomplete_fields = ("member",)
    readonly_fields = (
        "created_at",
        "updated_at",
        "reserved_at",
        "released_at",
        "released_by",
        "release_reason",
        "reserved_cash",
        "reserved_units",
        "reservation_nav",
        "redeemed_at",
        "redeemed_by",
        "redemption_nav",
    )


@admin.register(Asset)
class AssetAdmin(admin.ModelAdmin):
    list_display = ("title", "asset_type", "total_cost", "status", "created_by", "created_at")
    list_filter = ("asset_type", "status", "created_at")
    search_fields = ("title", "description", "created_by__full_name")
    readonly_fields = (
        "created_at",
        "updated_at",
        "cancelled_at",
        "cancelled_by",
        "cancellation_reason",
        "reopened_at",
        "reopened_by",
        "reopen_reason",
        "acquired_at",
        "acquired_units",
        "acquisition_unit_price",
    )
    inlines = (AssetCommitmentInline,)

    def save_model(self, request, obj, form, change):
        if not change and not obj.created_by_id:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)


@admin.register(AssetCommitment)
class AssetCommitmentAdmin(admin.ModelAdmin):
    list_display = ("asset", "member", "amount", "reserved_cash", "reserved_units", "reserved_at", "released_at", "redeemed_at")
    list_filter = ("reserved_at", "released_at")
    search_fields = ("asset__title", "member__full_name")
    readonly_fields = (
        "created_at",
        "updated_at",
        "reserved_at",
        "released_at",
        "released_by",
        "release_reason",
        "reserved_cash",
        "reserved_units",
        "reservation_nav",
        "redeemed_at",
        "redeemed_by",
        "redemption_nav",
    )


@admin.register(AssetValuation)
class AssetValuationAdmin(admin.ModelAdmin):
    list_display = ("asset", "as_of_date", "total_value", "unit_price", "recorded_by", "created_at")
    list_filter = ("as_of_date", "asset__asset_type")
    search_fields = ("asset__title", "recorded_by__full_name")
    readonly_fields = ("asset", "as_of_date", "total_value", "unit_price", "recorded_by", "created_at")

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

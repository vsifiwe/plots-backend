"""
URL configuration for core project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.1/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import include, path

from app.views import (
    AssetAcquireView,
    AssetAvailableBalanceView,
    AssetCancelView,
    AssetDetailView,
    AssetListCreateView,
    AssetOpenView,
    ContributionListCreateView,
    ContributionVoidView,
    MemberListCreateView,
)

urlpatterns = [
    path('admin/', admin.site.urls),
    path("api/auth/", include("app.urls")),
    path("api/members/", MemberListCreateView.as_view(), name="member-list-create"),
    path(
        "api/contributions/",
        ContributionListCreateView.as_view(),
        name="contribution-list-create",
    ),
    path(
        "api/contributions/<int:contribution_id>/void/",
        ContributionVoidView.as_view(),
        name="contribution-void",
    ),
    path("api/assets/", AssetListCreateView.as_view(), name="asset-list-create"),
    path("api/assets/available-balances/", AssetAvailableBalanceView.as_view(), name="asset-available-balances"),
    path("api/assets/<int:asset_id>/", AssetDetailView.as_view(), name="asset-detail"),
    path("api/assets/<int:asset_id>/open/", AssetOpenView.as_view(), name="asset-open"),
    path("api/assets/<int:asset_id>/acquire/", AssetAcquireView.as_view(), name="asset-acquire"),
    path("api/assets/<int:asset_id>/cancel/", AssetCancelView.as_view(), name="asset-cancel"),
]

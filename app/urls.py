from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from .views import LoginView, LogoutView, MemberDetailView


urlpatterns = [
    path("login/", LoginView.as_view(), name="login"),
    path("refresh/", TokenRefreshView.as_view(), name="token-refresh"),
    path("me/", MemberDetailView.as_view(), name="member-detail"),
    path("logout/", LogoutView.as_view(), name="logout"),
]

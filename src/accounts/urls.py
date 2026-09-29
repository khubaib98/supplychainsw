from django.urls import path

from accounts import views

app_name = "accounts"

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("login/verify/", views.login_2fa_view, name="login_2fa"),
    path("logout/", views.logout_view, name="logout"),
    path("account/", views.account_view, name="account"),
    path("account/security/", views.security_view, name="security"),
    path("account/password/", views.PortalPasswordChangeView.as_view(), name="password"),
]

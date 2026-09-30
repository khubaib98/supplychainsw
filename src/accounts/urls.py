from django.urls import path

from accounts import people_views, views

app_name = "accounts"

urlpatterns = [
    path("login/", views.login_view, name="login"),
    path("login/verify/", views.login_2fa_view, name="login_2fa"),
    path("logout/", views.logout_view, name="logout"),
    path("account/", views.account_view, name="account"),
    path("account/security/", views.security_view, name="security"),
    path("account/password/", views.PortalPasswordChangeView.as_view(), name="password"),
    path("setup/", people_views.setup_view, name="setup"),
    path("people/", people_views.people_view, name="people"),
    path("people/new/", people_views.person_create, name="person_new"),
    path("people/computer/", people_views.computer_settings, name="computer"),
    path("people/backup/", people_views.backup_now, name="backup_now"),
    path("people/<int:pk>/", people_views.person_edit, name="person"),
    path("people/<int:pk>/welcome/", people_views.person_welcome, name="person_welcome"),
    path("people/<int:pk>/action/", people_views.person_action, name="person_action"),
]

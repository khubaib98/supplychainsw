from django.contrib import admin
from django.urls import include, path
from django.views.generic import RedirectView

from accounts.views import set_language
from core.views import healthz

admin.site.site_header = "Consignment portal – system administration"
admin.site.site_title = "Consignment portal admin"

urlpatterns = [
    # Admin logins go through the portal login so 2FA and lock-out apply there too.
    path("admin/login/", RedirectView.as_view(pattern_name="accounts:login", query_string=True)),
    path("admin/", admin.site.urls),
    path("i18n/setlang/", set_language, name="set_language"),
    path("healthz/", healthz, name="healthz"),
    path("", include("accounts.urls")),
    path("", include("logistics.urls")),
]

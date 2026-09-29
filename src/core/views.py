import secrets

from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.views.decorators.cache import never_cache

# Changes every time the portal process starts (lets a page notice a finished restart).
BOOT_ID = secrets.token_hex(4)


@never_cache
def healthz(request):
    """Liveness check for uptime monitors and the desktop launcher (no login needed)."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return JsonResponse(
        {"status": "ok", "app": "cnpk-portal", "instance": settings.PORTAL["INSTANCE"], "boot": BOOT_ID}
    )

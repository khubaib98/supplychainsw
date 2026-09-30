from django.conf import settings

from core import translation as mt


def portal(request):
    context = {
        "PORTAL_NAME": settings.PORTAL["NAME"],
        "MT_ENABLED": mt.enabled(),
    }
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        context["unread_notifications"] = user.notifications.filter(read_at__isnull=True).count()
        context["my_profile"] = getattr(user, "profile", None)
    return context

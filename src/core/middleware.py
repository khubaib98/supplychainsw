import zoneinfo

from django.utils import timezone, translation

# Everything (scripts, styles, fonts) is served from this server: no Google
# Fonts, CDNs or analytics, which are slow or blocked in mainland China.
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "media-src 'self' blob: mediastream:",
        "connect-src 'self'",
        "font-src 'self'",
        "frame-ancestors 'none'",
        "form-action 'self'",
        "base-uri 'self'",
        "object-src 'none'",
    ]
)


class SecurityHeadersMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        # Camera is needed for QR scanning on phones; nothing else.
        response.headers.setdefault("Permissions-Policy", "camera=(self), microphone=(), geolocation=()")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        return response


class UserPreferencesMiddleware:
    """Activate the signed-in user's language and time zone for this request."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        profile = getattr(request.user, "profile", None) if request.user.is_authenticated else None
        if profile is not None:
            translation.activate(profile.language)
            request.LANGUAGE_CODE = translation.get_language()
            try:
                timezone.activate(zoneinfo.ZoneInfo(profile.timezone))
            except (zoneinfo.ZoneInfoNotFoundError, ValueError):
                timezone.deactivate()
        else:
            timezone.deactivate()
        return self.get_response(request)

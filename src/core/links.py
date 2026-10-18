"""
The addresses people use to open the portal. A server has PORTAL_BASE_URL; the
desktop launcher passes the addresses of its computer (PORTAL_ACCESS_URLS), and
the address of a temporary Cloudflare Tunnel, which can change while the portal
runs, comes from the launcher's tunnel.json (see core/tunnel.py).
"""
from django.conf import settings

from core import desktop


def tunnel_status():
    """What the launcher last reported about Cloudflare Tunnel; {} without a tunnel."""
    if not settings.PORTAL["DESKTOP"] or settings.PORTAL["TUNNEL"] == "off":
        return {}
    return desktop.read_tunnel_state(settings.DESKTOP_HOME, settings.PORTAL["TUNNEL_SESSION"])


def access_urls():
    """[{"kind": "local" | "network" | "public" | "temporary", "url": ...}]"""
    urls = list(settings.PORTAL["ACCESS_URLS"]) or [{"kind": "public", "url": settings.PORTAL["BASE_URL"]}]
    temporary = tunnel_status().get("url")
    if temporary:
        urls.append({"kind": "temporary", "url": temporary})
    return urls


def shareable_url():
    """The address to send to someone who is not sitting at this computer (None if there is none)."""
    urls = access_urls()
    for kind in ("public", "temporary", "network"):
        for entry in urls:
            if entry["kind"] == kind:
                return entry["url"]
    return None


def absolute(url):
    """A link for e-mails and chat messages."""
    if not url or url.startswith("http"):
        return url
    return (shareable_url() or settings.PORTAL["BASE_URL"]).rstrip("/") + url

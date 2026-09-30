"""Small helpers that build a realistic Shipper / Receiver / Oversight world for tests."""
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile

from accounts.models import Organization, Profile
from logistics import services
from logistics.models import ItemType

PASSWORD = "Test-Password-2026"
PDF = b"%PDF-1.4\n1 0 obj << >> endobj\ntrailer << >>\n%%EOF\n"


def user(username, organization, level=Profile.Level.OPERATOR, language="en", tz="UTC"):
    account = get_user_model().objects.create_user(username, f"{username}@example.com", PASSWORD)
    Profile.objects.create(user=account, organization=organization, level=level, language=language, timezone=tz)
    return account


def world():
    shipper = Organization.objects.create(name="Shipper Co", kind=Organization.Kind.SHIPPER, country="CN")
    receiver = Organization.objects.create(name="Receiver Ltd", kind=Organization.Kind.RECEIVER, country="PK")
    oversight = Organization.objects.create(name="Top Management", kind=Organization.Kind.OVERSIGHT, country="PK")
    other_shipper = Organization.objects.create(name="Other Shipper", kind=Organization.Kind.SHIPPER, country="CN")
    w = SimpleNamespace(
        shipper=shipper,
        receiver=receiver,
        oversight=oversight,
        other_shipper=other_shipper,
        s_mgr=user("s_mgr", shipper, Profile.Level.MANAGER, "zh-hans", "Asia/Shanghai"),
        s_ops=user("s_ops", shipper),
        s_view=user("s_view", shipper, Profile.Level.VIEWER),
        r_mgr=user("r_mgr", receiver, Profile.Level.MANAGER, "ur", "Asia/Karachi"),
        r_ops=user("r_ops", receiver),
        o_view=user("o_view", oversight, Profile.Level.VIEWER),
        x_mgr=user("x_mgr", other_shipper, Profile.Level.MANAGER),
    )
    w.dcjm = ItemType.objects.create(code="DCJM", name_en="Dual Channel Jamming Module", name_zh="双通道干扰模块",
                                     name_ur="ڈوئل چینل جیمنگ ماڈیول", controlled=True)
    w.psu = ItemType.objects.create(code="PSU", name_en="Power Supply Unit")
    w.cbl = ItemType.objects.create(code="CBL", name_en="RF Cable", tracking=ItemType.Tracking.BATCH)
    return w


def consignment(w, mode="sea", **extra):
    return services.create_consignment(
        w.s_mgr, shipper=w.shipper, receiver=w.receiver, transport_mode=mode,
        origin="Shenzhen", destination="Karachi", **extra,
    )


def advance(w, consignment_, *statuses):
    """Walk a consignment through milestones as the right party."""
    for status in statuses:
        actor = w.s_mgr if consignment_.STATUS_OWNER[status] == "shipper" else w.r_mgr
        services.set_status(actor, consignment_, status)


def pdf(name="invoice.pdf", content=PDF):
    return SimpleUploadedFile(name, content, content_type="application/pdf")

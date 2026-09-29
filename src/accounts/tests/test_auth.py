from django.conf import settings
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from accounts import totp
from core.models import AuditLog
from logistics.tests.factories import PASSWORD, world

# RFC 6238 test secret "12345678901234567890" in base32.
RFC_SECRET = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


class TotpTests(TestCase):
    def test_rfc6238_vectors(self):
        self.assertEqual(totp.code_at(RFC_SECRET, 59 // 30, digits=8), "94287082")
        self.assertEqual(totp.code_at(RFC_SECRET, 1111111109 // 30, digits=8), "07081804")

    def test_verify_accepts_neighbouring_steps_and_blocks_replay(self):
        now = 1_800_000_000
        step = totp.current_step(now)
        code = totp.code_at(RFC_SECRET, step)
        self.assertEqual(totp.verify(RFC_SECRET, code, now=now), step)
        self.assertEqual(totp.verify(RFC_SECRET, code, now=now + 30), step)
        self.assertIsNone(totp.verify(RFC_SECRET, code, last_used_step=step, now=now))
        self.assertIsNone(totp.verify(RFC_SECRET, "12345", now=now))
        self.assertIsNone(totp.verify(RFC_SECRET, "abcdef", now=now))

    def test_provisioning_uri(self):
        uri = totp.provisioning_uri("ABC", "s_mgr", "CN–PK Portal")
        self.assertTrue(uri.startswith("otpauth://totp/"))
        self.assertIn("secret=ABC", uri)


class LoginTests(TestCase):
    def setUp(self):
        cache.clear()
        self.w = world()

    def test_password_login_and_audit(self):
        response = self.client.post(reverse("accounts:login"), {"username": "s_ops", "password": PASSWORD})
        self.assertRedirects(response, reverse("logistics:dashboard"))
        self.assertTrue(AuditLog.objects.filter(action="auth.login", username="s_ops").exists())

    def test_two_factor_login(self):
        profile = self.w.s_mgr.profile
        profile.totp_secret = RFC_SECRET
        profile.totp_enabled = True
        profile.save()
        response = self.client.post(reverse("accounts:login"), {"username": "s_mgr", "password": PASSWORD})
        self.assertRedirects(response, reverse("accounts:login_2fa"))
        # Not logged in yet.
        self.assertEqual(self.client.get(reverse("logistics:dashboard")).status_code, 302)
        bad = self.client.post(reverse("accounts:login_2fa"), {"code": "000000"})
        self.assertEqual(bad.status_code, 200)
        code = totp.code_at(RFC_SECRET, totp.current_step())
        good = self.client.post(reverse("accounts:login_2fa"), {"code": code})
        self.assertRedirects(good, reverse("logistics:dashboard"))
        self.assertEqual(self.client.get(reverse("logistics:dashboard")).status_code, 200)

    def test_lockout_after_repeated_failures(self):
        for _attempt in range(5):
            self.client.post(reverse("accounts:login"), {"username": "s_ops", "password": "wrong"})
        response = self.client.post(reverse("accounts:login"), {"username": "s_ops", "password": PASSWORD})
        self.assertEqual(response.status_code, 200)  # still on the login page
        self.assertEqual(self.client.get(reverse("logistics:dashboard")).status_code, 302)
        self.assertEqual(AuditLog.objects.filter(action="auth.login_failed").count(), 5)

    def test_mandatory_2fa_redirects_to_setup(self):
        with self.settings(PORTAL={**settings.PORTAL, "REQUIRE_2FA": True}):
            self.client.force_login(self.w.s_ops)
            response = self.client.get(reverse("logistics:dashboard"))
            self.assertRedirects(response, reverse("accounts:security"))

    def test_language_choice_is_remembered_on_profile(self):
        self.client.force_login(self.w.s_ops)
        self.client.post(reverse("set_language"), {"language": "zh-hans", "next": "/"})
        self.w.s_ops.profile.refresh_from_db()
        self.assertEqual(self.w.s_ops.profile.language, "zh-hans")
        page = self.client.get(reverse("logistics:dashboard"))
        self.assertContains(page, "看板")

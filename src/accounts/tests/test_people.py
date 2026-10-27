import base64
import json
import re
import shutil
import tempfile
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from accounts import people
from accounts.models import Organization
from core import desktop, links
from core.models import AuditLog
from logistics.tests.factories import PASSWORD, world

User = get_user_model()
TOKEN = "one-time-setup-key"
NEW_PASSWORD = "Harbour-Lantern-2026"


def portal(**changes):
    return override_settings(PORTAL={**settings.PORTAL, **changes})


class SetupWizardTests(TestCase):
    def setUp(self):
        cache.clear()
        self.data = {
            "name": "Khubaib",
            "username": "admin",
            "email": "",
            "password1": NEW_PASSWORD,
            "password2": NEW_PASSWORD,
            "shipper_name": "Shenzhen Shipper Co.",
            "shipper_name_local": "深圳发货公司",
            "receiver_name": "Rawalpindi Receiver Ltd",
            "receiver_name_local": "",
            "oversight_name": "Top management",
        }

    def test_not_available_without_a_token(self):
        self.assertEqual(self.client.get(reverse("accounts:setup")).status_code, 404)

    @portal(SETUP_TOKEN=TOKEN)
    def test_needs_the_one_time_link(self):
        self.assertEqual(self.client.get(reverse("accounts:setup")).status_code, 403)
        self.assertEqual(self.client.get(reverse("accounts:setup") + "?token=wrong").status_code, 403)
        self.assertEqual(self.client.post(reverse("accounts:setup"), self.data).status_code, 403)
        self.assertFalse(User.objects.exists())

    @portal(SETUP_TOKEN=TOKEN)
    def test_creates_administrator_and_organisations(self):
        response = self.client.get(reverse("accounts:setup") + f"?token={TOKEN}")
        self.assertRedirects(response, reverse("accounts:setup"), fetch_redirect_response=False)
        self.assertContains(self.client.get(reverse("accounts:setup")), "shipper_name")
        response = self.client.post(reverse("accounts:setup"), self.data)
        self.assertRedirects(response, reverse("accounts:people"))
        admin = User.objects.get()
        self.assertTrue(admin.is_superuser)
        self.assertTrue(admin.check_password(NEW_PASSWORD))
        shipper = Organization.objects.get(kind=Organization.Kind.SHIPPER)
        self.assertEqual((shipper.name_local, shipper.default_language), ("深圳发货公司", "zh-hans"))
        self.assertEqual(Organization.objects.count(), 3)
        self.assertTrue(AuditLog.objects.filter(action="setup.completed").exists())
        # Once someone exists the page is closed for good.
        self.client.logout()
        response = self.client.get(reverse("accounts:setup") + f"?token={TOKEN}")
        self.assertRedirects(response, reverse("accounts:login"))

    @portal(SETUP_TOKEN=TOKEN)
    def test_rejects_weak_or_mismatched_passwords(self):
        self.client.get(reverse("accounts:setup") + f"?token={TOKEN}")
        weak = self.client.post(reverse("accounts:setup"), {**self.data, "password1": "12345", "password2": "12345"})
        self.assertEqual(weak.status_code, 200)
        mismatch = self.client.post(reverse("accounts:setup"), {**self.data, "password2": "Something-Else-2026"})
        self.assertEqual(mismatch.status_code, 200)
        self.assertFalse(User.objects.exists())


class PeopleTests(TestCase):
    def setUp(self):
        cache.clear()
        self.w = world()
        self.admin = User.objects.create_superuser("root", "root@example.com", PASSWORD)
        self.client.force_login(self.admin)

    def create(self, **extra):
        data = {
            "first_name": "Li", "last_name": "Wei", "username": "li.wei", "email": "",
            "organization": self.w.shipper.pk, "level": "operator", "language": "", "timezone": "",
            **extra,
        }
        return self.client.post(reverse("accounts:person_new"), data)

    def temporary_password(self, response):
        return re.search(r'id="temp-password">([^<]+)<', response.content.decode()).group(1)

    def test_only_administrators(self):
        self.client.force_login(self.w.s_mgr)
        self.assertEqual(self.client.get(reverse("accounts:people")).status_code, 403)
        self.assertEqual(self.create().status_code, 403)

    def test_lists_organisations_and_members(self):
        page = self.client.get(reverse("accounts:people"))
        self.assertContains(page, "Shipper Co")
        self.assertContains(page, "s_mgr")
        self.assertContains(page, "Top Management")

    def test_new_person_gets_a_temporary_password_once(self):
        self.w.shipper.default_language = "zh-hans"
        self.w.shipper.save()
        response = self.create()
        person = User.objects.get(username="li.wei")
        self.assertRedirects(response, reverse("accounts:person_welcome", args=[person.pk]), fetch_redirect_response=False)
        welcome = self.client.get(reverse("accounts:person_welcome", args=[person.pk]))
        password = self.temporary_password(welcome)
        self.assertRegex(password, r"^[a-z2-9]{4}-[a-z2-9]{4}-[a-z2-9]{4}$")
        self.assertTrue(person.check_password(password))
        # The message is written in the person's language (the Shipper's default: Chinese).
        self.assertContains(welcome, "临时密码")
        self.assertEqual(person.profile.language, "zh-hans")
        self.assertEqual(person.profile.timezone, "Asia/Shanghai")
        self.assertTrue(person.profile.must_change_password)
        again = self.client.get(reverse("accounts:person_welcome", args=[person.pk]))
        self.assertNotContains(again, password)
        self.assertTrue(AuditLog.objects.filter(action="people.created", target_label="li.wei").exists())

    def test_first_sign_in_asks_for_a_new_password(self):
        self.create()
        person = User.objects.get(username="li.wei")
        password = self.temporary_password(self.client.get(reverse("accounts:person_welcome", args=[person.pk])))
        self.client.logout()
        self.client.post(reverse("accounts:login"), {"username": "li.wei", "password": password})
        self.assertRedirects(self.client.get(reverse("logistics:dashboard")), reverse("accounts:password"))
        response = self.client.post(
            reverse("accounts:password"), {"new_password1": NEW_PASSWORD, "new_password2": NEW_PASSWORD}
        )
        self.assertRedirects(response, reverse("logistics:dashboard"))
        person.refresh_from_db()
        self.assertFalse(person.profile.must_change_password)
        self.assertTrue(person.check_password(NEW_PASSWORD))
        self.assertEqual(self.client.get(reverse("logistics:dashboard")).status_code, 200)

    def test_password_first_then_two_factor(self):
        profile = self.w.s_ops.profile
        profile.must_change_password = True
        profile.save()
        self.client.force_login(self.w.s_ops)
        with self.settings(PORTAL={**settings.PORTAL, "REQUIRE_2FA": True}):
            self.assertRedirects(self.client.get(reverse("logistics:dashboard")), reverse("accounts:password"))
            self.assertEqual(self.client.get(reverse("accounts:password")).status_code, 200)
            profile.must_change_password = False
            profile.save()
            self.assertRedirects(self.client.get(reverse("accounts:password")), reverse("accounts:security"))

    def test_username_must_be_unique(self):
        response = self.create(username="S_OPS")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(User.objects.filter(username="S_OPS").exists())

    def test_edit_person(self):
        url = reverse("accounts:person", args=[self.w.s_ops.pk])
        self.assertEqual(self.client.get(url).status_code, 200)
        response = self.client.post(url, {
            "first_name": "Liu", "last_name": "Yang", "username": "s_ops", "email": "liu@example.com",
            "organization": self.w.shipper.pk, "level": "manager", "language": "zh-hans", "timezone": "Asia/Shanghai",
        })
        self.assertRedirects(response, reverse("accounts:people"))
        self.w.s_ops.refresh_from_db()
        self.assertEqual((self.w.s_ops.profile.level, self.w.s_ops.email), ("manager", "liu@example.com"))

    def test_reset_password_signs_the_person_out_and_forces_a_new_one(self):
        url = reverse("accounts:person_action", args=[self.w.r_ops.pk])
        response = self.client.post(url, {"action": "reset_password"})
        welcome = self.client.get(response.url)
        password = self.temporary_password(welcome)
        self.w.r_ops.refresh_from_db()
        self.assertTrue(self.w.r_ops.check_password(password))
        self.assertFalse(self.w.r_ops.check_password(PASSWORD))
        self.assertTrue(self.w.r_ops.profile.must_change_password)

    def test_reset_two_factor(self):
        profile = self.w.r_mgr.profile
        profile.totp_enabled, profile.totp_secret = True, "ABC"
        profile.save()
        self.client.post(reverse("accounts:person_action", args=[self.w.r_mgr.pk]), {"action": "reset_2fa"})
        profile.refresh_from_db()
        self.assertEqual((profile.totp_enabled, profile.totp_secret), (False, ""))

    def test_deactivate_and_reactivate(self):
        url = reverse("accounts:person_action", args=[self.w.s_view.pk])
        self.client.post(url, {"action": "deactivate"})
        self.w.s_view.refresh_from_db()
        self.assertFalse(self.w.s_view.is_active)
        self.client.logout()
        self.client.post(reverse("accounts:login"), {"username": "s_view", "password": PASSWORD})
        self.assertEqual(self.client.get(reverse("logistics:dashboard")).status_code, 302)
        self.client.force_login(self.admin)
        self.client.post(url, {"action": "reactivate"})
        self.w.s_view.refresh_from_db()
        self.assertTrue(self.w.s_view.is_active)

    def test_cannot_deactivate_yourself(self):
        self.client.post(reverse("accounts:person_action", args=[self.admin.pk]), {"action": "deactivate"})
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_temporary_password_alphabet(self):
        for _attempt in range(50):
            password = people.temporary_password()
            self.assertEqual(len(password), 14)
            self.assertFalse(set(password) & set("01oli"))


class ComputerSettingsTests(TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        desktop.prepare_home(self.home)
        self.admin = User.objects.create_superuser("root", "root@example.com", PASSWORD)
        self.client.force_login(self.admin)
        urls = [{"kind": "local", "url": "http://127.0.0.1:8000"}]
        self.desktop_mode = override_settings(
            DESKTOP_HOME=str(self.home), PORTAL={**settings.PORTAL, "DESKTOP": True, "ACCESS_URLS": urls}
        )
        self.desktop_mode.enable()
        self.addCleanup(self.desktop_mode.disable)

    def test_people_page_shows_this_computer(self):
        page = self.client.get(reverse("accounts:people"))
        self.assertContains(page, str(self.home))
        self.assertContains(page, "http://127.0.0.1:8000")
        self.assertContains(page, "only this computer can open the portal")

    def test_saving_settings_updates_settings_ini(self):
        data = {"network_access": "on", "public_url": "https://portal.example.com/", "require_two_factor": "on"}
        response = self.client.post(reverse("accounts:computer"), data)
        self.assertRedirects(response, reverse("accounts:people"))  # no launcher here: restart by hand
        values = desktop.load_settings(self.home)
        self.assertEqual(
            (values["network_access"], values["public_url"], values["require_two_factor"]),
            (True, "https://portal.example.com", True),
        )
        self.assertTrue(AuditLog.objects.filter(action="settings.computer").exists())

    def test_restarts_when_started_by_the_launcher(self):
        with mock.patch("core.desktop.request_restart", return_value=True) as restart:
            response = self.client.post(reverse("accounts:computer"), {"public_url": ""})
        restart.assert_called_once()
        self.assertContains(response, "data-wait-restart")

    def test_rejects_a_public_address_with_a_path(self):
        self.client.post(reverse("accounts:computer"), {"public_url": "https://example.com/portal"})
        self.assertEqual(desktop.load_settings(self.home)["public_url"], "")

    def test_rejects_the_address_of_this_computer(self):
        for address in ("https://127.0.0.1:8000", "http://localhost:8000", "https://[::1]"):
            page = self.save(public_url=address)
            self.assertContains(page, "This is the address of this computer.")
            self.assertEqual(desktop.load_settings(self.home)["public_url"], "")

    def save(self, **data):
        return self.client.post(reverse("accounts:computer"), data, follow=True)

    def tunnel_token(self):
        content = {"a": "0123456789abcdef0123456789abcdef", "t": "5e1f2a7b-9c3d-4e5f-8a6b-7c8d9e0f1a2b", "s": "c2VjcmV0"}
        return base64.b64encode(json.dumps(content).encode()).decode()

    def test_temporary_tunnel(self):
        self.save(tunnel="temporary")
        self.assertEqual(desktop.load_settings(self.home)["tunnel"], "temporary")
        self.save()  # not sent means off, like the tick boxes
        self.assertEqual(desktop.load_settings(self.home)["tunnel"], "off")

    def test_own_address_needs_a_token_and_the_address(self):
        page = self.save(tunnel="token")
        self.assertContains(page, "Paste the tunnel token from the Cloudflare dashboard.")
        self.assertContains(page, "Enter the address you gave the tunnel in Cloudflare.")
        self.assertEqual(desktop.load_settings(self.home)["tunnel"], "off")
        page = self.save(tunnel="token", tunnel_token="hello", public_url="https://portal.example.com")
        self.assertContains(page, "This is not a tunnel token.")

    def test_token_is_kept_but_never_shown_or_logged(self):
        token = self.tunnel_token()
        self.save(tunnel="token", tunnel_token=f"cloudflared.exe service install {token}",
                  public_url="https://portal.example.com")
        values = desktop.load_settings(self.home)
        self.assertEqual((values["tunnel"], values["tunnel_token"]), ("token", token))
        page = self.client.get(reverse("accounts:people"))
        self.assertNotContains(page, token)
        self.assertContains(page, "A token is saved.")
        record = AuditLog.objects.get(action="settings.computer")
        self.assertEqual(record.details["tunnel_token"], "replaced")
        self.assertNotIn(token, json.dumps(record.details))
        self.save(tunnel="token", tunnel_token="", public_url="https://portal.example.com")  # keeps the token
        self.assertEqual(desktop.load_settings(self.home)["tunnel_token"], token)
        self.assertNotIn("tunnel_token", AuditLog.objects.filter(action="settings.computer").first().details)

    def tunnel_running(self, **state):
        desktop.write_tunnel_state(self.home, {"session": "s1", "mode": "temporary", **state})
        return self.settings(PORTAL={**settings.PORTAL, "DESKTOP": True, "TUNNEL": "temporary", "TUNNEL_SESSION": "s1",
                                     "ACCESS_URLS": [{"kind": "local", "url": "http://127.0.0.1:8000"}]})

    def test_people_page_shows_the_temporary_address(self):
        with self.tunnel_running(state="connected", url="https://brave-otter-words.trycloudflare.com"):
            page = self.client.get(reverse("accounts:people"))
            self.assertEqual(links.absolute("/consignments/"), "https://brave-otter-words.trycloudflare.com/consignments/")
        self.assertContains(page, "https://brave-otter-words.trycloudflare.com")
        self.assertContains(page, "From anywhere (temporary address)")
        self.assertContains(page, "Cloudflare Tunnel is connected.")
        self.assertContains(page, "a password alone is not enough")
        self.assertNotContains(page, "only this computer can open the portal")

    def test_people_page_explains_a_problem(self):
        with self.tunnel_running(state="failed", message="cloudflared could not be downloaded (timed out)."):
            page = self.client.get(reverse("accounts:people"))
        self.assertContains(page, "Cloudflare Tunnel is not working.")
        self.assertContains(page, "cloudflared could not be downloaded (timed out).")

    def test_state_of_an_earlier_launcher_is_ignored(self):
        desktop.write_tunnel_state(self.home, {"session": "old", "state": "connected",
                                               "url": "https://old-words.trycloudflare.com"})
        with self.tunnel_running(state="connected", url="https://brave-otter-words.trycloudflare.com"):
            desktop.write_tunnel_state(self.home, {"session": "old", "state": "connected",
                                                   "url": "https://old-words.trycloudflare.com"})
            page = self.client.get(reverse("accounts:people"))
        self.assertNotContains(page, "old-words")

    def test_back_up_now(self):
        self.client.post(reverse("accounts:backup_now"))
        self.assertEqual(len(desktop.list_backups(self.home / "backups")), 1)

    def test_not_available_outside_the_desktop_edition(self):
        with self.settings(PORTAL={**settings.PORTAL, "DESKTOP": False}):
            self.assertEqual(self.client.post(reverse("accounts:computer"), {}).status_code, 404)
            self.assertEqual(self.client.post(reverse("accounts:backup_now")).status_code, 404)


class HealthTests(TestCase):
    def test_healthz(self):
        response = self.client.get(reverse("healthz"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

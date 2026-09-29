import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from core import desktop


class DesktopTestCase(SimpleTestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        desktop.prepare_home(self.home)


class SettingsFileTests(DesktopTestCase):
    def test_created_with_defaults_and_comments(self):
        values = desktop.load_settings(self.home)
        self.assertEqual((values["port"], values["network_access"]), (8000, False))
        text = (self.home / "settings.ini").read_text(encoding="utf-8")
        self.assertIn("# no  = only this computer can open the portal", text)
        self.assertIn("network_access = no", text)

    def test_demo_uses_its_own_port(self):
        self.assertEqual(desktop.load_settings(self.home, demo=True)["port"], 8001)

    def test_update_keeps_other_values_and_comments(self):
        desktop.load_settings(self.home)
        path = self.home / "settings.ini"
        path.write_text(
            path.read_text(encoding="utf-8").replace("smtp_host = ", "smtp_host = mail.example.com")
            + "PORTAL_REMINDER_DAYS = 0,3,7\n",
            encoding="utf-8",
        )
        values = desktop.update_settings(self.home, network_access=True, public_url="https://portal.example.com/")
        self.assertEqual(values["public_url"], "https://portal.example.com")
        again = desktop.load_settings(self.home)
        self.assertTrue(again["network_access"])
        self.assertEqual(again["smtp_host"], "mail.example.com")
        self.assertEqual(again["advanced"], {"PORTAL_REMINDER_DAYS": "0,3,7"})
        self.assertIn("# Machine translation of remarks", path.read_text(encoding="utf-8"))

    def test_bad_values_are_explained(self):
        desktop.load_settings(self.home)
        path = self.home / "settings.ini"
        path.write_text(path.read_text(encoding="utf-8").replace("port = 8000", "port = eighty"), encoding="utf-8")
        with self.assertRaisesMessage(desktop.SettingsError, "port must be a whole number"):
            desktop.load_settings(self.home)

    def test_public_url(self):
        self.assertEqual(desktop.normalize_public_url(" https://a.example.com/ "), "https://a.example.com")
        self.assertEqual(desktop.normalize_public_url(""), "")
        for bad in ("portal.example.com", "ftp://a.example.com", "https://a.example.com/portal"):
            with self.subTest(bad=bad), self.assertRaises(desktop.SettingsError):
                desktop.normalize_public_url(bad)

    def test_secret_key_is_kept(self):
        first = desktop.secret_key(self.home)
        self.assertEqual(desktop.secret_key(self.home), first)
        self.assertEqual(desktop.instance_id(self.home), desktop.instance_id(self.home))
        if os.name != "nt":
            self.assertEqual((self.home / "secret.key").stat().st_mode & 0o777, 0o600)


class EnvironmentTests(DesktopTestCase):
    def test_local_only(self):
        values = desktop.update_settings(self.home, public_url="https://portal.example.com")
        env = desktop.django_environment(self.home, values, addresses=["192.168.1.20"])
        self.assertEqual(env["DJANGO_ALLOWED_HOSTS"], "localhost,127.0.0.1,[::1],portal.example.com")
        self.assertEqual(env["DJANGO_CSRF_TRUSTED_ORIGINS"], "https://portal.example.com")
        self.assertEqual(env["PORTAL_BASE_URL"], "https://portal.example.com")
        self.assertEqual(env["DJANGO_EMAIL_BACKEND"], "django.core.mail.backends.filebased.EmailBackend")
        kinds = [entry["kind"] for entry in json.loads(env["PORTAL_ACCESS_URLS"])]
        self.assertEqual(kinds, ["local", "public"])

    def test_network_access(self):
        values = desktop.update_settings(self.home, network_access=True, smtp_host="mail.example.com")
        env = desktop.django_environment(self.home, values, demo=True, addresses=["192.168.1.20"])
        self.assertEqual(env["DJANGO_ALLOWED_HOSTS"], "*")
        self.assertEqual(env["PORTAL_BASE_URL"], "http://192.168.1.20:8000")
        self.assertEqual(env["DJANGO_COOKIE_PREFIX"], "demo_")
        self.assertEqual(env["SMTP_HOST"], "mail.example.com")

    def test_restart_needs_the_launcher(self):
        with mock.patch.dict(os.environ, {desktop.SUPERVISED_ENV: ""}):
            self.assertFalse(desktop.request_restart())


class BackupTests(DesktopTestCase):
    def make_database(self, value):
        with closing(sqlite3.connect(self.home / "db.sqlite3")) as db:
            db.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
            db.execute("DELETE FROM t")
            db.execute("INSERT INTO t VALUES (?)", (value,))
            db.commit()

    def read_database(self):
        with closing(sqlite3.connect(self.home / "db.sqlite3")) as db:
            return db.execute("SELECT v FROM t").fetchone()[0]

    def test_backup_and_restore(self):
        self.make_database("original")
        (self.home / "media" / "docs").mkdir(parents=True)
        (self.home / "media" / "docs" / "invoice.pdf").write_bytes(b"%PDF-1.4 original")
        result = desktop.make_backup(self.home, now=datetime(2026, 9, 29, 2, 15))
        self.assertEqual(result.path.name, "portal-backup-20260929-021500.zip")
        with zipfile.ZipFile(result.path) as archive:
            self.assertEqual(
                sorted(archive.namelist()), ["backup-info.json", "db.sqlite3", "media/docs/invoice.pdf"]
            )
        self.make_database("changed")
        (self.home / "media" / "docs" / "invoice.pdf").write_bytes(b"changed")
        kept = desktop.restore_backup(self.home, result.path)
        self.assertEqual(self.read_database(), "original")
        self.assertEqual((self.home / "media" / "docs" / "invoice.pdf").read_bytes(), b"%PDF-1.4 original")
        self.assertEqual((kept / "media" / "docs" / "invoice.pdf").read_bytes(), b"changed")

    def test_copy_to_second_folder(self):
        self.make_database("x")
        second = self.home / "usb"
        result = desktop.make_backup(self.home, copy_to=str(second))
        self.assertEqual(result.copy_error, "")
        self.assertTrue((second / result.path.name).exists())

    def test_prune_keeps_the_newest(self):
        folder = self.home / "backups"
        start = datetime(2026, 1, 1)
        for day in range(20):
            (folder / f"portal-backup-{start + timedelta(days=day):%Y%m%d-%H%M%S}.zip").write_bytes(b"x")
        desktop.prune_backups(folder, keep_days=3, now=start + timedelta(days=100))
        self.assertEqual(len(desktop.list_backups(folder)), desktop.BACKUP_KEEP_AT_LEAST)
        desktop.prune_backups(folder, keep_days=3650, now=start + timedelta(days=100))
        self.assertEqual(len(desktop.list_backups(folder)), desktop.BACKUP_KEEP_AT_LEAST)

    def test_daily_backup_only_when_due(self):
        self.assertIsNone(desktop.backup_if_due(self.home, desktop.DEFAULTS))  # no database yet
        self.make_database("x")
        now = datetime(2026, 9, 29, 9, 0)
        self.assertIsNotNone(desktop.backup_if_due(self.home, desktop.DEFAULTS, now=now))
        self.assertIsNone(desktop.backup_if_due(self.home, desktop.DEFAULTS, now=now + timedelta(hours=5)))
        self.assertIsNotNone(desktop.backup_if_due(self.home, desktop.DEFAULTS, now=now + timedelta(hours=24)))
        self.assertEqual(desktop.latest_backup(self.home)[0], now + timedelta(hours=24))

    def test_restore_refuses_foreign_files(self):
        not_zip = self.home / "notes.zip"
        not_zip.write_text("hello")
        evil = self.home / "evil.zip"
        with zipfile.ZipFile(evil, "w") as archive:
            archive.writestr("db.sqlite3", b"")
            archive.writestr("../outside.txt", b"x")
        empty = self.home / "empty.zip"
        with zipfile.ZipFile(empty, "w") as archive:
            archive.writestr("media/a.pdf", b"x")
        for archive in (not_zip, evil, empty):
            with self.subTest(archive=archive.name), self.assertRaises(desktop.BackupError):
                desktop.restore_backup(self.home, archive)
        self.assertFalse((self.home.parent / "outside.txt").exists())

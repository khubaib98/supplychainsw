from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings

from core import audit
from core import translation as mt
from core.i18n import detect_language
from core.models import AuditLog, TextTranslation


class AuditChainTests(TestCase):
    def setUp(self):
        for index in range(5):
            audit.log(None, "test.event", None, label=f"event {index}", details={"n": index})

    def test_intact_chain_verifies(self):
        ok, checked, bad = audit.verify_chain()
        self.assertTrue(ok)
        self.assertEqual(checked, 5)
        call_command("verify_audit", stdout=open("/dev/null", "w"))

    def test_edited_history_is_detected(self):
        record = AuditLog.objects.order_by("id")[2]
        AuditLog.objects.filter(pk=record.pk).update(details={"n": 999})
        ok, _checked, bad = audit.verify_chain()
        self.assertFalse(ok)
        self.assertEqual(bad, record.pk)
        with self.assertRaises(CommandError):
            call_command("verify_audit")

    def test_deleted_record_is_detected(self):
        AuditLog.objects.order_by("id")[1].delete()
        self.assertFalse(audit.verify_chain()[0])

    def test_truncated_tail_is_detected(self):
        AuditLog.objects.order_by("-id").first().delete()
        self.assertFalse(audit.verify_chain()[0])


class LanguageDetectionTests(TestCase):
    def test_script_based_detection(self):
        self.assertEqual(detect_language("CTN-017 outer carton crushed"), "en")
        self.assertEqual(detect_language("CTN-017 外箱被压坏"), "zh-hans")
        self.assertEqual(detect_language("CTN-017 کا بیرونی کارٹن پچکا ہوا ہے"), "ur")
        self.assertEqual(detect_language("Please check the invoice from 张三 today"), "en")
        self.assertEqual(detect_language("12345"), "en")


class MachineTranslationTests(TestCase):
    def setUp(self):
        mt.reset_cooldown()

    @override_settings(TRANSLATION={"PROVIDER": "none"})
    def test_no_provider_does_nothing(self):
        self.assertEqual(mt.translate_and_store("Hello"), 0)
        self.assertFalse(TextTranslation.objects.exists())

    @override_settings(TRANSLATION={"PROVIDER": "dummy"})
    def test_translations_are_stored_once_for_the_other_languages(self):
        self.assertEqual(mt.translate_and_store("Carton crushed", "en"), 2)
        self.assertEqual(mt.translate_and_store("Carton crushed", "en"), 0)
        self.assertEqual(mt.lookup("Carton crushed", "en", "zh-hans").text, "[zh-hans] Carton crushed")
        self.assertEqual(mt.lookup("Carton crushed", "en", "ur").text, "[ur] Carton crushed")
        self.assertIsNone(mt.lookup("Carton crushed", "en", "en"))

    @override_settings(TRANSLATION={"PROVIDER": "libretranslate", "LIBRETRANSLATE_URL": "http://127.0.0.1:9",
                                    "LIBRETRANSLATE_API_KEY": "", "TIMEOUT": 1})
    def test_provider_outage_never_breaks_the_caller(self):
        with self.assertLogs("core.translation", level="WARNING"):
            self.assertEqual(mt.translate_and_store("Hello there", "en"), 0)
        # During the cool-down no further calls are attempted.
        self.assertEqual(mt.translate_and_store("Another text", "en"), 0)

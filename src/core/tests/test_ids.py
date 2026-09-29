from django.test import TestCase, override_settings

from core import ids


class SequenceTests(TestCase):
    def test_reserve_hands_out_consecutive_blocks(self):
        self.assertEqual(ids.reserve("k"), 1)
        self.assertEqual(ids.reserve("k", 40), 2)
        self.assertEqual(ids.reserve("k"), 42)
        self.assertEqual(ids.reserve("other"), 1)

    def test_bump_to_never_goes_backwards(self):
        ids.bump_to("k", 57)
        ids.bump_to("k", 3)
        self.assertEqual(ids.reserve("k"), 58)

    @override_settings(PORTAL={"CONSIGNMENT_PREFIX": "CSG"})
    def test_consignment_code_format(self):
        self.assertEqual(ids.next_consignment_code(2026), "CSG-2026-0001")
        self.assertEqual(ids.next_consignment_code(2026), "CSG-2026-0002")
        self.assertEqual(ids.next_consignment_code(2027), "CSG-2027-0001")


class CodeTests(TestCase):
    def test_child_and_short_codes(self):
        self.assertEqual(ids.child_code("CSG-2026-0042", "CTN", 17), "CSG-2026-0042-CTN-017")
        self.assertEqual(ids.child_code("CSG-2026-0042", "DCJM", 1234), "CSG-2026-0042-DCJM-1234")
        self.assertEqual(ids.short_code("CSG-2026-0042-DCJM-017"), "DCJM-017")
        self.assertEqual(ids.short_code("not-a-code"), "not-a-code")

    def test_normalize_scan_handles_scanner_and_ime_quirks(self):
        # Full-width characters typed through a Chinese input method.
        self.assertEqual(ids.normalize_scan("ＣＳＧ－２０２６－０００２－ＣＴＮ－０１７"), "CSG-2026-0002-CTN-017")
        self.assertEqual(ids.normalize_scan("  csg-2026-0002-dcjm-017\n"), "CSG-2026-0002-DCJM-017")
        self.assertEqual(ids.normalize_scan("CSG–2026–0002"), "CSG-2026-0002")  # en dashes
        self.assertEqual(ids.normalize_scan("https://portal.example.com/i/CSG-2026-0002-DCJM-017/"), "CSG-2026-0002-DCJM-017")
        self.assertEqual(ids.normalize_scan(None), "")

    def test_classify(self):
        self.assertEqual(ids.classify("CSG-2026-0002")[0], "consignment")
        self.assertEqual(ids.classify("CSG-2026-0002-CTN-017")[0], "child")
        self.assertIsNone(ids.classify("CTN-001/057")[0])

    def test_iso6346_check_digit(self):
        self.assertTrue(ids.iso6346_valid("MSKU1234565"))
        self.assertTrue(ids.iso6346_valid("CSQU3054383"))
        self.assertTrue(ids.iso6346_valid("TGHU 837421-5"))
        self.assertFalse(ids.iso6346_valid("MSKU1234566"))
        self.assertFalse(ids.iso6346_valid("ABC1234567"))

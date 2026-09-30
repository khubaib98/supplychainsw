from django.db import migrations

# code, English, Chinese, Urdu, uploaded by, required by default, modes, due at milestone, +days, order
DOCUMENT_TYPES = [
    ("CI", "Commercial Invoice", "商业发票", "کمرشل انوائس", "shipper", True, "", "ready", 0, 10),
    ("PL", "Packing List", "装箱单", "پیکنگ لسٹ", "shipper", True, "", "ready", 0, 20),
    ("BL", "Bill of Lading", "海运提单", "بل آف لیڈنگ", "shipper", True, "sea", "in_transit", 3, 30),
    ("AWB", "Air Waybill", "航空运单", "ایئر وے بل", "shipper", True, "air", "in_transit", 1, 31),
    ("RCN", "Road Consignment Note / TIR Carnet", "公路运单 / TIR 单证", "روڈ کنسائنمنٹ نوٹ / ٹی آئی آر کارنیٹ",
     "shipper", True, "road", "in_transit", 1, 32),
    ("PAY", "Payment Documents", "付款单据", "ادائیگی کی دستاویزات", "shipper", True, "", "dispatched", 7, 40),
    ("COO", "Certificate of Origin (CPFTA Form P)", "原产地证书（中巴自贸协定 Form P）", "سرٹیفکیٹ آف اوریجن (CPFTA فارم P)",
     "shipper", False, "", "in_transit", 3, 50),
    ("INS", "Insurance Certificate", "保险单", "انشورنس سرٹیفکیٹ", "shipper", False, "", "dispatched", 0, 60),
    ("EXL", "Export Licence (dual-use items)", "出口许可证（两用物项）", "ایکسپورٹ لائسنس (دوہرے استعمال کی اشیاء)",
     "shipper", False, "", "ready", 0, 70),
    ("EUC", "End-User Certificate / NOC", "最终用户证明 / 无异议证书 (NOC)", "اینڈ یوزر سرٹیفکیٹ / این او سی",
     "receiver", False, "", "packing", 0, 80),
    ("FI", "Financial Instrument (PSW)", "金融工具登记（巴基斯坦单一窗口 PSW）", "فنانشل انسٹرومنٹ (PSW)",
     "receiver", False, "", "dispatched", 0, 90),
    ("GD", "Goods Declaration (Pakistan Customs)", "进口报关单（巴基斯坦海关 GD）", "گڈز ڈیکلریشن (پاکستان کسٹمز)",
     "receiver", False, "", "arrived", 3, 100),
    ("GRN", "Goods Received Note", "收货单", "گڈز ریسیوڈ نوٹ", "receiver", False, "", "delivered", 2, 110),
    ("OTH", "Other", "其他", "دیگر", "any", False, "", "draft", 0, 999),
]


def create_types(apps, schema_editor):
    DocumentType = apps.get_model("logistics", "DocumentType")
    for code, en, zh, ur, responsible, required, modes, due_status, offset, order in DOCUMENT_TYPES:
        DocumentType.objects.update_or_create(
            code=code,
            defaults={
                "name_en": en,
                "name_zh": zh,
                "name_ur": ur,
                "responsible": responsible,
                "default_required": required,
                "modes": modes,
                "due_status": due_status,
                "due_offset_days": offset,
                "sort_order": order,
            },
        )


def remove_types(apps, schema_editor):
    apps.get_model("logistics", "DocumentType").objects.filter(code__in=[row[0] for row in DOCUMENT_TYPES]).delete()


class Migration(migrations.Migration):
    dependencies = [("logistics", "0001_initial")]

    operations = [migrations.RunPython(create_types, remove_types)]

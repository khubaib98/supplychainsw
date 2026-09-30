from django.core.management.base import BaseCommand, CommandError

from core.audit import verify_chain


class Command(BaseCommand):
    help = "Verify the audit-log hash chain (detects edited or deleted history)."

    def handle(self, *args, **options):
        ok, checked, bad_id = verify_chain()
        if not ok:
            where = f" at record #{bad_id}" if bad_id else " (chain head does not match the last record)"
            raise CommandError(f"Audit chain BROKEN{where} after {checked} valid records.")
        self.stdout.write(self.style.SUCCESS(f"Audit chain intact: {checked} records verified."))

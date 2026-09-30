"""
Background jobs, run by the `scheduler` container:
  * missing-document reminders and escalations
  * retrying machine translations that failed earlier (e.g. provider was down)
"""
import logging
import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core import translation as mt

logger = logging.getLogger(__name__)


def retry_translations(days=30):
    from logistics.models import Message, Thread

    if not mt.enabled():
        return 0
    since = timezone.now() - timedelta(days=days)
    created = 0
    for thread in Thread.objects.filter(updated_at__gte=since):
        created += mt.translate_and_store(thread.subject, thread.subject_lang)
    for message in Message.objects.filter(created_at__gte=since):
        created += mt.translate_and_store(message.body, message.lang)
    return created


class Command(BaseCommand):
    help = "Send document reminders and retry pending translations (loop, or once with --once)."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run one cycle and exit (for cron).")
        parser.add_argument("--interval", type=int, default=900, help="Seconds between cycles (default 900).")

    def handle(self, *args, **options):
        from logistics.reminders import run_reminders

        while True:
            started = timezone.now()
            try:
                sent = run_reminders()
                mt.reset_cooldown()
                translated = retry_translations()
                self.stdout.write(f"{started:%Y-%m-%d %H:%M} reminders sent: {len(sent)}, translations added: {translated}")
            except Exception:  # noqa: BLE001 - keep the loop alive; the next cycle retries
                logger.exception("Scheduler cycle failed")
            if options["once"]:
                return
            time.sleep(max(options["interval"], 60))

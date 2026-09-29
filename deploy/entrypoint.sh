#!/bin/sh
# web       : apply migrations, then serve with gunicorn (Caddy terminates HTTPS in front)
# scheduler : reminders, escalations and translation retries every 15 minutes
# anything else is executed as given, e.g. `python manage.py createsuperuser`
set -e

case "${1:-web}" in
  web)
    python manage.py migrate --noinput
    exec gunicorn config.wsgi:application \
      --bind 0.0.0.0:8000 \
      --workers "${GUNICORN_WORKERS:-3}" \
      --timeout 90 \
      --forwarded-allow-ips "*" \
      --access-logfile - \
      --error-logfile -
    ;;
  scheduler)
    exec python manage.py run_scheduler --interval "${SCHEDULER_INTERVAL:-900}"
    ;;
  *)
    exec "$@"
    ;;
esac

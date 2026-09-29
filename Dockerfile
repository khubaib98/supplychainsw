# syntax=docker/dockerfile:1
# Portal image: one image, two roles (web / scheduler), selected by the command.

FROM python:3.12-slim AS build
ENV PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt /tmp/requirements.txt
RUN pip install --prefix=/install -r /tmp/requirements.txt && pip install polib==1.2.0
COPY src/ /app/
COPY deploy/compile_po.py /tmp/compile_po.py
# Translations and static files are built into the image; nothing is fetched at runtime.
RUN python /tmp/compile_po.py /app/locale \
 && PYTHONPATH=/install/lib/python3.12/site-packages DJANGO_SECRET_KEY=build-only PORTAL_DATA_DIR=/tmp/build \
      DJANGO_STATIC_MANIFEST=1 python manage.py collectstatic --noinput \
 && rm -rf /app/data

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    DJANGO_STATIC_MANIFEST=1 PORTAL_DATA_DIR=/data PORTAL_MEDIA_ROOT=/data/media
COPY --from=build /install /usr/local
COPY --from=build /app /app
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod 755 /entrypoint.sh \
 && useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin portal \
 && mkdir -p /data/media && chown -R portal:portal /data
WORKDIR /app
USER portal
EXPOSE 8000
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/login/', timeout=4)" || exit 1
ENTRYPOINT ["/entrypoint.sh"]
CMD ["web"]

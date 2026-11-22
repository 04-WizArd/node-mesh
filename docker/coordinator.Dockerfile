# syntax=docker/dockerfile:1
# Build from the repository root:  docker build -f docker/coordinator.Dockerfile .
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NODEMESH_ENV_FILE="" \
    NODEMESH_SPOOL_DIR=/var/lib/nodemesh/spool

RUN useradd --system --uid 10001 --user-group --home-dir /app --shell /usr/sbin/nologin nodemesh

WORKDIR /app
COPY requirements/coordinator.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt
COPY shared /opt/nodemesh-shared
RUN pip install /opt/nodemesh-shared

COPY coordinator/ /app/
COPY docker/coordinator-entrypoint.sh /usr/local/bin/nodemesh-entrypoint
# collectstatic only needs importable settings; these values exist solely for the build.
RUN DJANGO_SECRET_KEY=collectstatic-only POSTGRES_HOST=unused POSTGRES_DB=unused \
    POSTGRES_USER=unused POSTGRES_PASSWORD=unused \
    python manage.py collectstatic --noinput --verbosity 0 \
 && mkdir -p /var/lib/nodemesh/spool \
 && chown -R nodemesh:nodemesh /var/lib/nodemesh \
 && chmod 0755 /usr/local/bin/nodemesh-entrypoint

USER nodemesh
EXPOSE 8000
# Readiness, not just liveness: healthy only while the database is reachable.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=4)"]
ENTRYPOINT ["nodemesh-entrypoint"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--threads", "4", "--timeout", "300", "--graceful-timeout", "30", "--access-logfile", "-"]

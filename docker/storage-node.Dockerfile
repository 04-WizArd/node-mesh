# syntax=docker/dockerfile:1
# One image for every storage node; identity and keys come from the environment.
# Build from the repository root:  docker build -f docker/storage-node.Dockerfile .
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NODE_DATA_DIR=/data

RUN useradd --system --uid 10002 --user-group --home-dir /app --shell /usr/sbin/nologin nodemesh

WORKDIR /app
COPY requirements/storage-node.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt
COPY shared /opt/nodemesh-shared
RUN pip install /opt/nodemesh-shared

COPY storage-node/ /app/
COPY docker/storage-node-entrypoint.sh /usr/local/bin/nodemesh-entrypoint
# The volume is initialised from this directory, so it inherits the ownership.
RUN mkdir -p /data && chown nodemesh:nodemesh /data && chmod 0700 /data \
 && chmod 0755 /usr/local/bin/nodemesh-entrypoint
VOLUME ["/data"]

USER nodemesh
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=4)"]
ENTRYPOINT ["nodemesh-entrypoint"]
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "2", "--threads", "4", "--timeout", "300", "--graceful-timeout", "30"]

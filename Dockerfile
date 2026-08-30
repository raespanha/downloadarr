FROM python:3.14-slim@sha256:cae66f2ef0ec51a9891263eeee7f987dacf0a9879e8aa9353d5606e0530619a5

ARG UID=1000
ARG GID=1000
ARG VERSION=0.1.0
ARG VCS_REF=unknown
ARG BUILD_DATE=unknown

LABEL org.opencontainers.image.title="Downloadarr" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.source="https://github.com/raespanha/downloadarr"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOWNLOADARR_CONFIG=/config/settings.json \
    DOWNLOADARR_VERSION=${VERSION} \
    PYTHONPATH=/app/src

RUN groupadd --gid "${GID}" downloadarr \
    && useradd --uid "${UID}" --gid "${GID}" --create-home downloadarr

WORKDIR /app
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.lock
COPY src ./src

RUN mkdir -p /config /downloads \
    && chown -R downloadarr:downloadarr /app /config /downloads

USER downloadarr
EXPOSE 6500

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:6500/readyz', timeout=3).read()"]

CMD ["python", "-c", "from downloadarr.api.app import main; main()"]

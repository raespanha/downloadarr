FROM python:3.12-slim

ARG UID=1000
ARG GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DOWNLOADARR_CONFIG=/config/settings.json

RUN groupadd --gid "${GID}" downloadarr \
    && useradd --uid "${UID}" --gid "${GID}" --create-home downloadarr

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

RUN mkdir -p /config /torbox \
    && chown -R downloadarr:downloadarr /app /config /torbox

USER downloadarr
EXPOSE 6500

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:6500/healthz', timeout=3).read()"]

CMD ["downloadarr-api"]

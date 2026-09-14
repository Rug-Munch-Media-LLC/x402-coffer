# ─────────────────────────────────────────────────────────────
# Coffer — self-hosted x402 payment facilitator (standalone image)
# Slim Python 3.12, non-root, installs the `x402-coffer` package
# (coffer/ + coffer_client/ + vendor/ per [tool.hatch.build.targets.wheel])
# and serves the /v1/x402 HTTP surface via uvicorn.
# ─────────────────────────────────────────────────────────────
FROM python:3.14-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /build

RUN pip install --upgrade pip setuptools wheel hatchling
COPY pyproject.toml ./
# hatchling validates package metadata at build time: pyproject references
# README.md (readme) and LICENSE (license file) — both must be present.
COPY README.md LICENSE ./
COPY coffer ./coffer
COPY coffer_client ./coffer_client
COPY vendor ./vendor
RUN pip wheel --no-deps --wheel-dir /wheels .

# ── Runtime image ─────────────────────────────────────────────
FROM python:3.14-slim AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    X402_HOST=0.0.0.0 X402_PORT=8001

RUN groupadd --system coffer && useradd --system --gid coffer --home-dir /srv/coffer coffer \
    && mkdir -p /srv/coffer && chown -R coffer:coffer /srv/coffer

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl \
    && rm -rf /wheels

USER coffer
WORKDIR /srv/coffer
EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8001/v1/x402/health')" || exit 1

CMD ["uvicorn", "--factory", "coffer._engine.config:create_app", \
     "--host", "0.0.0.0", "--port", "8001"]

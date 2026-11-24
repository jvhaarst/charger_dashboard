# Charge point availability dashboard (NDW DOT-NL).
#
# Stock multi-arch Python image, so this builds for arm64 (the Raspberry Pi k3s
# nodes) as well as amd64. uv installs dependencies from the lockfile.
FROM python:3.15-slim@sha256:63bb298d2b7b88f176f51b5189f54ce6535c997682ea70885d2132f5d473a8a5

COPY --from=ghcr.io/astral-sh/uv:latest@sha256:d3b601f0c7b118bc1753febf7c41cc80981df678445e0254284eecf84db79a7d /uv /uvx /bin/

WORKDIR /app

# Dependencies first, so a code change doesn't rebuild the layer.
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-dev --frozen --no-install-project 2>/dev/null || uv sync --no-dev

# Every module, not a list that a new file can be forgotten from — which is
# exactly how ocpi.py shipped missing. .dockerignore keeps test_app.py and
# seed_demo.py out.
COPY *.py ./
COPY templates/ ./templates/

# History and the response cache live here; mount a volume to keep them.
ENV NDW_DB=/data/history.sqlite3 \
    NDW_CACHE_DIR=/data/cache \
    PORT=8000
VOLUME ["/data"]
EXPOSE 8000

# One worker: the poller thread and the SQLite writer want a single process.
# Scaling out is fine too — the store ignores a minute it already holds.
CMD ["uv", "run", "--no-dev", "gunicorn", "--bind", "0.0.0.0:8000", \
     "--workers", "1", "--threads", "4", "--timeout", "60", "app:app"]

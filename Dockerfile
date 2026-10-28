# Charge point availability dashboard (NDW DOT-NL).
#
# Stock multi-arch Python image, so this builds for arm64 (the Raspberry Pi k3s
# nodes) as well as amd64. uv installs dependencies from the lockfile.
FROM python:3.15-slim@sha256:94d3426f1def7db4a106a232ac121bfd975393e5dfd40a619b955fae45fe7913

COPY --from=ghcr.io/astral-sh/uv:latest@sha256:af22a52f5eafef3d78e551599d405c02df61919d18028f0cea8eda93bc9986d0 /uv /uvx /bin/

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

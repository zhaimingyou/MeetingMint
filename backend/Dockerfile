# syntax=docker/dockerfile:1
# Backend image (task 14.1) -- serves BOTH the Backend_API (uvicorn) and the
# Job_Processor (Celery worker); docker-compose selects the role via `command`.
# Targets linux/arm64 (the DGX Spark). ffmpeg/ffprobe are required at runtime:
# the worker transcodes audio (preprocessing) and the API uses ffprobe for
# content-based format detection and duration probing.
#
# IMPORTANT layout note: app.migrations_runner resolves alembic.ini and the
# migrations/ directory RELATIVE TO the app package (Path(app.__file__)../..).
# So the image must keep app/, migrations/, and alembic.ini side by side under
# /app, and the package is installed EDITABLE (-e) so `import app` resolves to
# /app/app rather than site-packages. A non-editable install would break that
# path resolution.
# Target platform for the base image. Defaults to linux/arm64 (the DGX Spark);
# override with --build-arg TARGET_PLATFORM=linux/amd64 to build on an x86_64
# host (e.g. the 192.0.2.10 deployment). docker-compose.yml passes this
# through from the DOCKER_PLATFORM env var.
ARG TARGET_PLATFORM=linux/arm64
FROM --platform=${TARGET_PLATFORM} python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg bundles ffprobe; curl is used by the compose healthcheck.
# Swap to a domestic debian mirror FIRST — the DGX Spark / .26 sit behind the
# GFW and deb.debian.org stalls for 10+ minutes per large package. aliyun covers
# both the deb822 (.sources) and classic (sources.list) formats; it's a harmless
# no-op if a file is absent on some base image.
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true \
    ; sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list 2>/dev/null || true \
    ; apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the project preserving the backend layout (app/, migrations/, alembic.ini
# beside one another) then install dependencies + the app in editable mode.
COPY pyproject.toml README.md ./
COPY app ./app
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
# Use a BuildKit pip cache mount so wheels survive across (re)builds and retries
# -- the DGX's link to PyPI is slow/flaky, and without a cache every code change
# (which busts this layer) re-downloads all deps from scratch. A generous
# timeout/retries lets a slow mirror complete. PIP_NO_CACHE_DIR=0 re-enables the
# cache for this RUN (the global ENV disables it to keep the final image small).
# Pre-install build tools so source-dist builds (e.g. Mako during alembic
# dependency resolution) never need to download setuptools/wheel from PyPI.
# Use a domestic mirror — DGX sits behind the GFW and direct access to
# files.pythonhosted.org yields ~10 kB/s with frequent timeouts.
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple
RUN --mount=type=cache,target=/root/.cache/pip \
    PIP_NO_CACHE_DIR=0 pip install -i ${PIP_INDEX_URL} --timeout 120 --retries 10 setuptools wheel \
    && PIP_NO_CACHE_DIR=0 pip install -i ${PIP_INDEX_URL} --timeout 120 --retries 10 -e .

# Run as a non-root user; ensure the shared-volume mount point is owned by it.
# (The named volume is mounted empty at runtime, so the dirs themselves are
# (re)created by `python -m app.startup`; this chown covers the unmounted case.)
RUN useradd --system --uid 10001 --create-home appuser \
    && mkdir -p /data/db /data/audio \
    && chown -R appuser:appuser /data /app
USER appuser

EXPOSE 8000

# Default role: the API. The worker overrides `command` in docker-compose.yml.
CMD ["sh", "-c", "python -m app.entrypoint && uvicorn app.main:app --host 0.0.0.0 --port 8000"]

# Multi-stage Dockerfile for ValhallaRoute
# Stage 1: Build virtual environment with uv
FROM python:3.12-slim-bookworm AS builder

# Install uv binary from official image
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Set build environment
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Copy dependency specifications first to leverage Docker layer caching
COPY pyproject.toml uv.lock ./

# Install project dependencies into virtual environment
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Copy application source code and install project package
COPY valhallaroute/ valhallaroute/
COPY README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ------------------------------------------------------------------------------
# Stage 2: Runtime Image
# ------------------------------------------------------------------------------
FROM python:3.12-slim-bookworm AS runtime

# Install essential runtime shared libraries & CA certs for cloud object stores
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    libsqlite3-0 \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user and tile cache directory
RUN groupadd -g 10001 valhalla && \
    useradd -u 10001 -g valhalla -s /bin/bash -m valhalla && \
    mkdir -p /app /mnt/tile-cache && \
    chown -R valhalla:valhalla /app /mnt/tile-cache

WORKDIR /app

# Copy virtual environment and installed app from builder
COPY --from=builder --chown=valhalla:valhalla /app/.venv /app/.venv
COPY --from=builder --chown=valhalla:valhalla /app/valhallaroute /app/valhallaroute

# Set runtime environment variables
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Switch to non-root user
USER valhalla:valhalla

# Default entrypoint to valhallaroute CLI
ENTRYPOINT ["valhallaroute"]
CMD ["--help"]

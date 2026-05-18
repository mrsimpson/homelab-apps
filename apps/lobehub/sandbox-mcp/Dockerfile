# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# sandbox-mcp — Python MCP server wrapping sandlock for code execution
# ---------------------------------------------------------------------------
# Build: docker buildx build --platform linux/amd64 -t ghcr.io/<owner>/lobehub-sandbox-mcp .
# Run:   docker run --rm -p 8888:8888 ghcr.io/<owner>/lobehub-sandbox-mcp

FROM python:3.12-slim AS builder

# Build dependencies for sandlock (Rust FFI compiled via setuptools_rust) and mcp SDK
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gcc \
    libc6-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Rust toolchain (required to compile sandlock's native Rust extension)
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:$PATH
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --no-modify-path --default-toolchain stable --profile minimal

# Building from a fork branch that fixes the getppid()==1 false positive
# in containers (see https://github.com/multikernel/sandlock/pull/49).
# TODO: switch back to `pip install sandlock` once upstream fix is confirmed.
RUN git clone --depth 1 --branch diag/log-do-create-error https://github.com/mrsimpson/sandlock /build/sandlock \
    && git -C /build/sandlock log --oneline -1

# Build and install sandlock from source, then install the mcp SDK
RUN pip install --no-cache-dir setuptools setuptools_rust \
    && pip install --no-cache-dir --prefix=/install /build/sandlock/python "mcp[cli]"

# ---------------------------------------------------------------------------
# Final image — copy compiled packages only, no build tools
# ---------------------------------------------------------------------------
FROM python:3.12-slim

# Copy installed packages from builder stage.
# --prefix=/install puts libs at /install/lib/python3.12/site-packages and
# scripts at /install/bin — both need to land in /usr/local to be on PATH.
COPY --from=builder /install/lib /usr/local/lib
COPY --from=builder /install/bin /usr/local/bin

# Verify sandlock Python module is importable
RUN python3 -c "from sandlock import Sandbox, landlock_abi_version; print('sandlock ok, abi:', landlock_abi_version())"

WORKDIR /app
COPY server.py /app/server.py

# Run as non-root user (UID 1000)
RUN useradd --create-home --uid 1000 sandbox
USER sandbox

EXPOSE 8888

CMD ["python", "/app/server.py"]

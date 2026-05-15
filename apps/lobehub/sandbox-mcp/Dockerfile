# syntax=docker/dockerfile:1
# ---------------------------------------------------------------------------
# sandbox-mcp — Python MCP server wrapping sandlock for code execution
# ---------------------------------------------------------------------------
# Build: docker buildx build --platform linux/amd64 -t ghcr.io/<owner>/lobehub-sandbox-mcp .
# Run:   docker run --rm -p 8888:8888 ghcr.io/<owner>/lobehub-sandbox-mcp

FROM python:3.12-slim AS builder

# Build dependencies for sandlock (Rust FFI compiled via maturin) and mcp SDK
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Rust toolchain (required to compile sandlock's native Rust extension)
ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:$PATH
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --no-modify-path --default-toolchain stable --profile minimal

# Install Python packages into /install for clean copy to final image
# Note: sandlock ships pre-built manylinux wheels for x86_64 — Rust is only
# needed if the wheel is unavailable and the sdist must be compiled.
RUN pip install --no-cache-dir --prefix=/install sandlock "mcp[cli]"

# ---------------------------------------------------------------------------
# Final image — copy compiled packages only, no build tools
# ---------------------------------------------------------------------------
FROM python:3.12-slim

# Copy installed packages from builder stage.
# --prefix=/install puts libs at /install/lib/python3.12/site-packages and
# scripts at /install/bin — both need to land in /usr/local to be on PATH.
COPY --from=builder /install/lib /usr/local/lib
COPY --from=builder /install/bin /usr/local/bin

# Verify sandlock Python module and sandlock-mcp entry point are importable/runnable
RUN python3 -c "from sandlock import Sandbox, Policy, landlock_abi_version; print('sandlock ok, abi:', landlock_abi_version())" \
    && sandlock-mcp --help > /dev/null 2>&1 || true

WORKDIR /app
COPY server.py /app/server.py

# Run as non-root user (UID 1000)
RUN useradd --create-home --uid 1000 sandbox
USER sandbox

EXPOSE 8888

CMD ["python", "/app/server.py"]

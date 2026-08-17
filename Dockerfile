# Use the official uv image with Python 3.12
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# Set the shell to bash and enable pipefail for safer pipes
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    xz-utils \
    ca-certificates \
    procps \
    psmisc \
    git \
    openjdk-17-jdk-headless \
    build-essential \
    cmake \
    pkg-config \
    protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*

# Install Go 1.25.0
ENV GO_VERSION=1.25.0
RUN curl -L https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz | tar -C /usr/local -xz
ENV PATH=$PATH:/usr/local/go/bin

# Install protoc plugins for Go. Required by the launcher's codegen preparer
# for Go peers (test_suite/launcher/codegen.py::prepare_go) — without them,
# fresh CHECKOUT trees of a2a-go can't be built. Versions pinned so cache
# keys stay stable across image rebuilds.
ENV GOBIN=/root/go/bin
ENV PATH=$PATH:/root/go/bin
RUN go install google.golang.org/protobuf/cmd/protoc-gen-go@v1.36.11 && \
    go install google.golang.org/grpc/cmd/protoc-gen-go-grpc@v1.6.2

# Install Node.js v20.11.1
ENV NODE_VERSION=v20.11.1
RUN curl -L https://nodejs.org/dist/${NODE_VERSION}/node-${NODE_VERSION}-linux-x64.tar.xz | tar -xJ -C /usr/local --strip-components=1

# Install .NET SDK 8.0
RUN curl -sSL https://dot.net/v1/dotnet-install.sh | bash /dev/stdin --channel 8.0 --install-dir /usr/local/dotnet
ENV PATH=$PATH:/usr/local/dotnet
ENV DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1

# Install Rust 1.85.0 (minimum required by a2a-lf crate family)
ENV RUST_VERSION=1.85.0
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | \
    sh -s -- -y --default-toolchain ${RUST_VERSION} --no-modify-path
ENV PATH=$PATH:/root/.cargo/bin

# Install Maven 3.9.9 (to satisfy protobuf-maven-plugin 3.9.6+ requirement)
ENV MAVEN_VERSION=3.9.9
RUN curl -sSL https://archive.apache.org/dist/maven/maven-3/${MAVEN_VERSION}/binaries/apache-maven-${MAVEN_VERSION}-bin.tar.gz | tar -xz -C /usr/local
ENV PATH=$PATH:/usr/local/apache-maven-${MAVEN_VERSION}/bin

# Set the working directory
WORKDIR /app

ENV PYTHONPATH=/app
ENV UV_INDEX_URL=https://pypi.org/simple

# Dependency manifests only, ahead of the source. `uv sync` installs the
# project itself editable — no source ends up in the wheel, imports resolve
# through PYTHONPATH — so it needs nothing but these three files, and this
# layer then survives every source-only change. README.md is not optional:
# pyproject.toml declares `readme = "README.md"` and hatchling errors out
# without it.
COPY pyproject.toml uv.lock README.md /app/

# Materialize the itk service's own venv at build time so the CMD doesn't
# pay first-launch install cost, and pre-warm uv's wheel cache with the
# codegen preparer's grpcio-tools dependency. The peer-side `uv run
# --with grpcio-tools python -m grpc_tools.protoc` call in
# test_suite/launcher/codegen.py::prepare_python then reuses the cached
# wheels instead of fetching ~30 MB from PyPI per launcher invocation.
RUN uv sync --frozen && \
    uv run --with grpcio-tools python -c "import grpc_tools"

# Now the source. Anything excluded by .dockerignore (tests, scripts,
# dashboard) never reaches this layer, so editing it can't invalidate the
# build. We assume `docker build -t itk_service .` from the repo root.
COPY . /app

# Go and Node binaries are installed globally for JIT use

# Expose the service port
EXPOSE 8000

# Set environment variables if needed
ENV PYTHONUNBUFFERED=1

# Which service script to run. The legacy `itk_service.py` is gone (every
# SDK cut over to the launcher-based service); the env var stays so an
# operator can point the container at an alternative handler without
# rebuilding, and because every SDK's run_itk.sh already sets it.
ENV ITK_ENTRYPOINT=itk_service_v2.py

# `exec` so signals go straight to `uv run` (the shell never lingers in
# the process tree). `sh -c` is required to expand $ITK_ENTRYPOINT at
# container-start time — a JSON-form CMD would treat the env var as a
# literal string.
CMD ["sh", "-c", "exec uv run $ITK_ENTRYPOINT"]

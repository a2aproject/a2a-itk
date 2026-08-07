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

# Pre-build the Rust v1.0 agent so the binary is cached in the image layer.
# This avoids a cold Cargo compile at test runtime.
# Mirror the repo structure so build.rs can resolve ../../../protos correctly.
COPY protos /tmp/protos
COPY agents/rust/v10 /tmp/agents/rust/v10
WORKDIR /tmp/agents/rust/v10
RUN cargo build --release && \
    mkdir -p /app/agents/rust/v10/target/release && \
    cp target/release/itk-rust-v10-agent /app/agents/rust/v10/target/release/ && \
    rm -rf target
WORKDIR /app

# Install Maven 3.9.9 (to satisfy protobuf-maven-plugin 3.9.6+ requirement)
ENV MAVEN_VERSION=3.9.9
RUN curl -sSL https://archive.apache.org/dist/maven/maven-3/${MAVEN_VERSION}/binaries/apache-maven-${MAVEN_VERSION}-bin.tar.gz | tar -xz -C /usr/local
ENV PATH=$PATH:/usr/local/apache-maven-${MAVEN_VERSION}/bin

# Set the working directory
WORKDIR /app

# We assume the user runs docker build -t itk_service -f Dockerfile .
# inside the itk/ directory.
COPY . /app

# Install Python dependencies using uv (JIT during run)
ENV PYTHONPATH=/app
ENV UV_INDEX_URL=https://pypi.org/simple

# Materialize the itk service's own venv at build time so the CMD doesn't
# pay first-launch install cost, and pre-warm uv's wheel cache with the
# codegen preparer's grpcio-tools dependency. The peer-side `uv run
# --with grpcio-tools python -m grpc_tools.protoc` call in
# test_suite/launcher/codegen.py::prepare_python then reuses the cached
# wheels instead of fetching ~30 MB from PyPI per launcher invocation.
RUN uv sync --frozen && \
    uv run --with grpcio-tools python -c "import grpc_tools"

# Go and Node binaries are installed globally for JIT use

# Expose the service port
EXPOSE 8000

# Set environment variables if needed
ENV PYTHONUNBUFFERED=1

# Which service script to run — swap at `docker run` time.
#
# The launcher's new pipeline (test_suite/launcher/*) will eventually ship
# as `itk_service_v2.py`; until it lands as the default, this container
# defaults to the legacy `itk_service.py`. To run the new service instead:
#
#     docker run -e ITK_ENTRYPOINT=itk_service_v2.py itk_service
#
# Both entrypoints share the same image (identical toolchains, baked venv,
# and launcher code) — only the top-level HTTP handler differs. Keeping
# them in one image avoids maintaining two ~90-line Dockerfiles that would
# drift.
ENV ITK_ENTRYPOINT=itk_service.py

# `exec` so signals go straight to `uv run` (the shell never lingers in
# the process tree). `sh -c` is required to expand $ITK_ENTRYPOINT at
# container-start time — a JSON-form CMD would treat the env var as a
# literal string.
CMD ["sh", "-c", "exec uv run $ITK_ENTRYPOINT"]

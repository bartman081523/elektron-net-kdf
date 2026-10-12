# syntax=docker/dockerfile:1
# elektron-net marketplace container (TESTNET MODE).
#
# One container runs the whole stack:
#   kdf  --config market -> market-maker daemon (i_am_seed, RPC 127.0.0.1:7795)
#   kdf  --config trader -> visitor-facing daemon (RPC 127.0.0.1:7796)
#   elek-web             -> SPA + same-origin proxy on 0.0.0.0:$PORT (healthcheck /healthz)
#
# Test-Mode-by-default: the only coins enabled are TESTNET coins (tBTC from
# public electrum servers; tELEK from the in-container electrs). Self-contained
# by default: the runtime also carries the local testnet infrastructure binaries
# (elektrond node + electrs indexer) and mines its own chain — the container has
# no host dependencies. Before the web service is exposed, docker/selftest.py
# runs the no-custody API matrix and the deployment FAILS on any hard failure.
#
# Build:  docker build --build-arg KDF_BUILD_TAG=<sha> -t elektron-market .
#         (the node binaries come from the extra build context "bin":
#          --build-context bin=/path/to/staging with `elektrond` + `electrs`)
# Run:    docker run -p 10000:10000 -e MM_TEST_SEED=... elektron-market
# See docker/README.md for the full env-var contract.

FROM rust:1-slim-bookworm AS builder
ENV RUSTC_BOOTSTRAP=1 \
    CARGO_TERM_COLOR=never \
    CARGO_NET_GIT_FETCH_WITH_CLI=true
WORKDIR /src
RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential protobuf-compiler \
    && rm -rf /var/lib/apt/lists/*
# mm2_bin_lib/build.rs stamps KDF_VERSION as "<crate-version>_<tag>": with
# KDF_BUILD_TAG set it is used directly, otherwise the script runs `git log`
# against a repository that is NOT copied into the builder stage and panics.
# CI passes the commit SHA; plain builds fall back to the default below.
ARG KDF_BUILD_TAG=elektron-docker
ENV KDF_BUILD_TAG=${KDF_BUILD_TAG}
COPY Cargo.toml Cargo.lock rust-toolchain.toml ./
COPY mm2src ./mm2src
# Build only the two binaries the container needs (daemon + web server).
# Cargo caches live in BuildKit cache mounts so repeated builds stay fast,
# and the finished binaries are copied out of the mount while it is still
# active (contents of a cache mount are not part of the image layer).
RUN --mount=type=cache,target=/src/target \
    --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    cargo build --release --locked -p mm2_bin_lib -p elek-web \
    && mkdir -p /out \
    && cp target/release/kdf target/release/elek-web /out/

# ---------------------------------------------------------------------------
# A1b: elektrond (the Elektron fork node) built FROM SOURCE in-image.
# Sources: bartman081523/elektron-net branch fork-main (public). Built on the
# same Arch toolchain it runs on, so no glibc mismatch. Only the daemon is
# needed (the miner loop mines via RPC; no CUDA miner binary in the image).
# ---------------------------------------------------------------------------
FROM archlinux:base AS elektron-build
RUN pacman -Syu --noconfirm --needed \
        base-devel cmake ninja git gcc \
        boost libevent sqlite \
    && rm -rf /var/cache/pacman/pkg /var/lib/pacman/sync
RUN git clone --depth 1 --branch fork-main \
        https://github.com/bartman081523/elektron-net.git /src
RUN mkdir -p /out \
    && cmake -S /src -B /build -G Ninja \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_BITCOIN_BIN=ON -DBUILD_DAEMON=ON -DBUILD_CLI=OFF \
        -DBUILD_GUI=OFF -DBUILD_WALLET_TOOL=OFF -DBUILD_UTIL=OFF \
        -DBUILD_TX=OFF -DBUILD_TESTS=OFF -DBUILD_BENCH=OFF \
        -DBUILD_FUZZ_BINARY=OFF -DBUILD_FOR_FUZZING=OFF \
        -DBUILD_KERNEL_LIB=OFF -DENABLE_IPC=OFF \
    && cmake --build /build --target elektrond -j"$(nproc)" \
    && cp /build/bin/elektrond /out/

# ---------------------------------------------------------------------------
# A1b: electrs (fork indexer) built FROM SOURCE in-image on a Debian rust
# toolchain (rocksdb needs a C++ toolchain; runtime glibc is newer and
# forward-compatible).
# ---------------------------------------------------------------------------
FROM rust:1-slim-bookworm AS electrs-build
ENV RUSTC_BOOTSTRAP=1 CARGO_TERM_COLOR=never
WORKDIR /src
RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential clang \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch elektron/61-market-api \
        https://github.com/bartman081523/elektron-net-electrs.git /src
RUN --mount=type=cache,target=/src/target \
    --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    cargo build --release \
    && mkdir -p /out \
    && cp /src/target/release/electrs /out/

# The runtime base is Arch (glibc 2.44, libstdc++ GLIBCXX_3.4.35) because the
# node binaries are built ON ARCH toolchains in-image (A1b): elektrond requires
# GLIBC_2.38 + GLIBCXX_3.4.35 + CXXABI_1.3.15 — bookworm (glibc 2.36, gcc 12)
# cannot provide those, the same-Arch in-image build can. kdf/elek-web (built on
# bookworm glibc 2.36) are forward-compatible on the newer glibc.
FROM archlinux:base
RUN pacman -Syu --noconfirm \
        ca-certificates python curl \
        libevent sqlite gcc-libs \
    && rm -rf /var/cache/pacman/pkg /var/lib/pacman/sync
COPY --from=builder /out/kdf /usr/local/bin/kdf
COPY --from=builder /out/elek-web /usr/local/bin/elek-web
# Node binaries of the self-contained testnet infra are built IN-IMAGE (A1b) —
# no extra build context needed anymore; both stages cache-compat in BuildKit.
COPY --from=elektron-build /out/elektrond /usr/local/bin/elektrond
COPY --from=electrs-build /out/electrs /usr/local/bin/electrs
COPY web /app/web
COPY coins /app/coins
COPY docker /app/docker
ENV MM_WEB_ROOT=/app/web \
    MM_STATE_DIR=/run/elek \
    MM_COINS_SRC=/app/docker/coins-testnet.json \
    MM_TELEK_TEMPLATE=/app/docker/telek-template.json \
    MM_LOCAL_TESTNET=1 \
    PORT=10000
EXPOSE 10000
ENTRYPOINT ["/app/docker/entrypoint.sh"]
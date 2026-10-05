# syntax=docker/dockerfile:1
# elektron-net marketplace container (TESTNET MODE).
#
# One container runs the whole stack:
#   kdf  --config market -> market-maker daemon (i_am_seed, RPC 127.0.0.1:7795)
#   kdf  --config trader -> visitor-facing daemon (RPC 127.0.0.1:7796)
#   elek-web             -> SPA + same-origin proxy on 0.0.0.0:$PORT (healthcheck /healthz)
#
# Test-Mode-by-default: the only coins enabled are TESTNET coins (tBTC from
# public electrum servers; tELEK only when MM_TELEK_ELECTRS is set). Before the
# web service is exposed, docker/selftest.py runs a full read-write API matrix
# and the deployment FAILS on any hard failure.
#
# Build:  docker build -t elektron-market .
# Run:    docker run -p 10000:10000 -e MM_TEST_SEED=... elektron-market
# See docker/README.md for the full env-var contract.

FROM rust:1-slim-bookworm AS builder
ENV RUSTC_BOOTSTRAP=1 \
    CARGO_TERM_COLOR=never \
    CARGO_NET_GIT_FETCH_WITH_CLI=true
WORKDIR /src
RUN apt-get update \
    && apt-get install -y --no-install-recommends git build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY Cargo.toml Cargo.lock rust-toolchain.toml ./
COPY mm2src ./mm2src
# Build only the two binaries the container needs (daemon + web server).
# Cargo caches live in BuildKit cache mounts so repeated builds stay fast,
# and the finished binaries are copied out of the mount while it is still
# active (contents of a cache mount are not part of the image layer).
RUN --mount=type=cache,target=/src/target \
    --mount=type=cache,target=/usr/local/cargo/registry \
    --mount=type=cache,target=/usr/local/cargo/git \
    cargo build --release --locked -p mm2_bin_lib -p elek_web \
    && mkdir -p /out \
    && cp target/release/kdf target/release/elek-web /out/

FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /out/kdf /usr/local/bin/kdf
COPY --from=builder /out/elek-web /usr/local/bin/elek-web
COPY web /app/web
COPY coins /app/coins
COPY docker /app/docker
ENV MM_WEB_ROOT=/app/web \
    MM_STATE_DIR=/run/elek \
    MM_COINS_SRC=/app/docker/coins-testnet.json \
    MM_TELEK_TEMPLATE=/app/docker/telek-template.json \
    PORT=10000
EXPOSE 10000
ENTRYPOINT ["/app/docker/entrypoint.sh"]
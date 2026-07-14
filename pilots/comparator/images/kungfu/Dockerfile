ARG KUNGFU_BUILDER_IMAGE=ghcr.io/kungfu-systems/build-images/native-linux-x64@sha256:a9313d8911e4f81af46436784f7a6a6c436b5d0f040ac9607204cbc91634a1ba
FROM ${KUNGFU_BUILDER_IMAGE} AS builder

ARG KUNGFU_SOURCE_REPOSITORY=https://github.com/kungfu-systems/kungfu.git
ARG KUNGFU_SOURCE_SHA=5b16a62b63e9a84ebea23f1a14b6253f66e3a8ab
ARG RUSTUP_VERSION=1.28.2
ARG RUSTUP_INIT_SHA256=20a06e644b0d9bd2fbdbfd52d42540bdde820ea7df86e92e533c073da0cdd43c
ARG RUST_TOOLCHAIN=1.96.0

ENV CARGO_HOME=/home/kungfu/.cargo
ENV RUSTUP_HOME=/home/kungfu/.rustup
ENV PATH=/home/kungfu/bin:/home/kungfu/.cargo/bin:/home/kungfu/.cache/kungfu/tools/uv/0.11.23/linux-x86_64:${PATH}
ENV CC=clang
ENV CXX=clang++

USER kungfu
WORKDIR /work

# native-linux-x64 intentionally omits unzip. Shifu needs only the conventional
# `unzip -oq ARCHIVE -d DEST` surface for its checksum-verified bootstrap zips,
# which Python's pinned base-image standard library can provide without apt.
RUN mkdir -p /home/kungfu/bin \
    && printf '%s\n' \
      '#!/bin/sh' \
      'set -eu' \
      'archive=' \
      'dest=.' \
      'while [ "$#" -gt 0 ]; do' \
      '  case "$1" in' \
      '    -oq|-qo|-o|-q) ;;' \
      '    -d) shift; dest=$1 ;;' \
      '    *) archive=$1 ;;' \
      '  esac' \
      '  shift' \
      'done' \
      'test -n "$archive"' \
      'exec python3 -m zipfile -e "$archive" "$dest"' \
      >/home/kungfu/bin/unzip \
    && chmod +x /home/kungfu/bin/unzip

RUN curl -fsSLo /tmp/rustup-init \
      "https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/x86_64-unknown-linux-gnu/rustup-init" \
    && echo "${RUSTUP_INIT_SHA256}  /tmp/rustup-init" | sha256sum -c - \
    && chmod +x /tmp/rustup-init \
    && /tmp/rustup-init -y --profile minimal --default-toolchain "${RUST_TOOLCHAIN}" \
    && rm -f /tmp/rustup-init \
    && rustc --version \
    && cargo --version

RUN git init /work/kungfu \
    && cd /work/kungfu \
    && git remote add origin "${KUNGFU_SOURCE_REPOSITORY}" \
    && git fetch --depth=1 origin "${KUNGFU_SOURCE_SHA}" \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "${KUNGFU_SOURCE_SHA}"

WORKDIR /work/kungfu

# Build only the source-backed core/CLI surface used by this unscored pilot.
# The source checkout owns the Node, pnpm, uv, Python, Cargo, Conan and product
# dependency locks. Clang 18 is the source contract's qualified Linux secondary
# compiler; setup duration remains visible in the hosted smoke job.
RUN clang --version \
    && clang++ --version \
    && ./shifu sync \
    && cd framework/core \
    && uv run --frozen conan profile detect --force \
    && cd ../.. \
    && ./shifu build:core \
    && ./shifu freeze \
    && test -x framework/core/dist/kungfu/kungfu \
    && framework/core/dist/kungfu/kungfu agent capabilities --json >/tmp/kungfu-capabilities.json

FROM ghcr.io/kungfu-systems/build-images/kungfu-verify@sha256:cb6d939d567c129903d6e3ad858e2c09a9475b3b8f81b1aad10279d0e63a920d

ARG KUNGFU_SOURCE_SHA=5b16a62b63e9a84ebea23f1a14b6253f66e3a8ab

USER root
COPY --from=builder /work/kungfu/framework/core/dist/kungfu /opt/kungfu
RUN mkdir -p /var/lib/kungfu \
    && chown -R kungfu:kungfu /var/lib/kungfu \
    && printf '%s\n' "${KUNGFU_SOURCE_SHA}" >/opt/kungfu/SOURCE_SHA \
    && chmod -R a-w /opt/kungfu

ENV PATH=/opt/kungfu:${PATH}
USER kungfu
WORKDIR /var/lib/kungfu

HEALTHCHECK --interval=2s --timeout=5s --retries=30 \
  CMD kungfu --home /tmp/kungfu-health agent capabilities --json >/dev/null || exit 1

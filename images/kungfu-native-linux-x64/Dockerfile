ARG BASE_IMAGE=ghcr.io/kungfu-systems/build-images/kungfu-verify:local
FROM ${BASE_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive
ARG APT_HTTP_PROXY=""
ARG FNM_VERSION=1.39.0
ARG FNM_SHA256=7807664f39d39fc518da1c35ba0181e4b3267603c4b1dedeb4b5fc6ae440a224
ARG UV_VERSION=0.11.23
ARG UV_SHA256=e12c4cda2fe8c305510a78380a88f2c32a27e90cdcd123cefd2873388f0ebb5f
ARG RUSTUP_VERSION=1.28.2
ARG RUSTUP_SHA256=20a06e644b0d9bd2fbdbfd52d42540bdde820ea7df86e92e533c073da0cdd43c
ARG RUST_TOOLCHAIN=1.96.0

USER root

RUN if [ -n "${APT_HTTP_PROXY}" ]; then \
      printf '%s\n' "Acquire::http::Proxy \"${APT_HTTP_PROXY}\";" > /etc/apt/apt.conf.d/01kungfu-build-cache; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
      build-essential \
      ccache \
      clang \
      cmake \
      file \
      g++-14 \
      gcc-14 \
      ninja-build \
      pkg-config \
    && curl -fsSLo /tmp/fnm.zip \
      "https://github.com/Schniz/fnm/releases/download/v${FNM_VERSION}/fnm-linux.zip" \
    && echo "${FNM_SHA256}  /tmp/fnm.zip" | sha256sum -c - \
    && unzip -qo /tmp/fnm.zip -d /usr/local/bin \
    && chmod 0755 /usr/local/bin/fnm \
    && curl -fsSLo /tmp/uv.tar.gz \
      "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-x86_64-unknown-linux-gnu.tar.gz" \
    && echo "${UV_SHA256}  /tmp/uv.tar.gz" | sha256sum -c - \
    && mkdir -p /tmp/uv \
    && tar -xzf /tmp/uv.tar.gz --strip-components=1 -C /tmp/uv \
    && install -m 0755 /tmp/uv/uv /tmp/uv/uvx /usr/local/bin/ \
    && curl -fsSLo /tmp/rustup-init \
      "https://static.rust-lang.org/rustup/archive/${RUSTUP_VERSION}/x86_64-unknown-linux-gnu/rustup-init" \
    && echo "${RUSTUP_SHA256}  /tmp/rustup-init" | sha256sum -c - \
    && chmod 0755 /tmp/rustup-init \
    && RUSTUP_HOME=/opt/rustup CARGO_HOME=/opt/rust-cargo \
      /tmp/rustup-init -y --no-modify-path --profile minimal --default-toolchain "${RUST_TOOLCHAIN}" \
    && for tool in cargo rustc rustdoc rustup; do \
      ln -sf "/opt/rust-cargo/bin/${tool}" "/usr/local/bin/${tool}"; \
    done \
    && rm -rf /tmp/fnm.zip /tmp/uv.tar.gz /tmp/uv /tmp/rustup-init \
    && rm -f /etc/apt/apt.conf.d/01kungfu-build-cache \
    && rm -rf /var/lib/apt/lists/*

COPY bin /opt/kungfu-native-source-build/bin
COPY tests /opt/kungfu-native-source-build/tests
RUN chmod 0755 \
      /opt/kungfu-native-source-build/bin/build-kungfu-core \
      /opt/kungfu-native-source-build/tests/smoke.sh

ENV CC=gcc-14
ENV CXX=g++-14
ENV COREPACK_NPM_REGISTRY=https://registry.npmjs.org
ENV PNPM_STORE_DIR=/home/kungfu/.cache/pnpm/store
ENV npm_config_devdir=/home/kungfu/.cache/node-gyp
ENV COREPACK_HOME=/home/kungfu/.cache/corepack
ENV FNM_DIR=/home/kungfu/.cache/fnm
ENV UV_CACHE_DIR=/home/kungfu/.cache/uv
ENV UV_PYTHON_INSTALL_DIR=/home/kungfu/.cache/uv-python
ENV CONAN_HOME=/home/kungfu/.cache/conan2
ENV CARGO_HOME=/home/kungfu/.cache/cargo
ENV RUSTUP_HOME=/opt/rustup
ENV RUSTUP_TOOLCHAIN=1.96.0
ENV KF_LIBWASM_CARGO_REGISTRY=sparse+https://index.crates.io/
ENV KUNGFU_BUILD_JOBS=12

USER kungfu
WORKDIR /work

LABEL io.kungfu.native-source-build.authority="build-reproducibility-only"
LABEL io.kungfu.native-source-build.node="22.22.3"
LABEL io.kungfu.native-source-build.rust="1.96.0"

ARG BASE_IMAGE=ghcr.io/kungfu-systems/build-images/base-linux:local
FROM ${BASE_IMAGE}

ARG NODE_VERSION=24.0.0
ARG NODE_SHA256=59b8af617dccd7f9f68cc8451b2aee1e86d6bd5cb92cd51dd6216a31b707efd7
ARG PNPM_VERSION=11.7.0

USER root

RUN curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
    && echo "${NODE_SHA256}  node-v${NODE_VERSION}-linux-x64.tar.xz" | sha256sum -c - \
    && tar -xJf "node-v${NODE_VERSION}-linux-x64.tar.xz" -C /usr/local --strip-components=1 \
    && rm "node-v${NODE_VERSION}-linux-x64.tar.xz" \
    && corepack enable \
    && npm install -g "pnpm@${PNPM_VERSION}"

USER kungfu
WORKDIR /work

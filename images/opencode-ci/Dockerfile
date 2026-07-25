ARG BASE_IMAGE=ghcr.io/kungfu-systems/build-images/kungfu-verify:local
FROM ${BASE_IMAGE}

ARG NODE_VERSION=24.0.0
ARG NODE_SHA256=59b8af617dccd7f9f68cc8451b2aee1e86d6bd5cb92cd51dd6216a31b707efd7
ARG PNPM_VERSION=11.7.0
ARG OPENCODE_VERSION=1.18.5
ARG OPENCODE_INTEGRITY=sha512-Q0jlX4ihn7veMeYsLX3c4PYFAKIURU3GIpXt1FnhNxNn3v8+RpIZ8z9umG5D0r8g8Smp9fZLGjgLe/9mJ4NyYw==

USER root

RUN curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
    && echo "${NODE_SHA256}  node-v${NODE_VERSION}-linux-x64.tar.xz" | sha256sum -c - \
    && tar -xJf "node-v${NODE_VERSION}-linux-x64.tar.xz" -C /usr/local --strip-components=1 \
    && rm "node-v${NODE_VERSION}-linux-x64.tar.xz" \
    && observed_integrity="$(npm view "opencode-ai@${OPENCODE_VERSION}" dist.integrity --registry=https://registry.npmjs.org)" \
    && test "${observed_integrity}" = "${OPENCODE_INTEGRITY}" \
    && npm install -g --registry=https://registry.npmjs.org \
      "pnpm@${PNPM_VERSION}" "opencode-ai@${OPENCODE_VERSION}" \
    && test "$(opencode --version)" = "${OPENCODE_VERSION}" \
    && npm cache clean --force

COPY bin/opencode-ci-run /usr/local/bin/opencode-ci-run
COPY bin/opencode-ci-verify /usr/local/bin/opencode-ci-verify
COPY tests/expected.txt /opt/opencode-ci/expected.txt

RUN chmod 0555 /usr/local/bin/opencode-ci-run /usr/local/bin/opencode-ci-verify \
    && mkdir -p /home/kungfu/.cache /home/kungfu/.config /home/kungfu/.local/share /home/kungfu/.local/state \
    && chown -R kungfu:kungfu /home/kungfu

ENV HOME=/home/kungfu
ENV XDG_CACHE_HOME=/home/kungfu/.cache
ENV XDG_CONFIG_HOME=/home/kungfu/.config
ENV XDG_DATA_HOME=/home/kungfu/.local/share
ENV XDG_STATE_HOME=/home/kungfu/.local/state

USER kungfu
WORKDIR /work

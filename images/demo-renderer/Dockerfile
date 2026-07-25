FROM mcr.microsoft.com/playwright:v1.54.2-noble@sha256:18b4bcff4f8ba0ac8c44b09f09def6a4f6cb8579e5f26381c21f38b50935d5d8

ARG NODE_VERSION=24.0.0
ARG NODE_SHA256=59b8af617dccd7f9f68cc8451b2aee1e86d6bd5cb92cd51dd6216a31b707efd7
ARG PLAYWRIGHT_VERSION=1.54.2
ARG PLAYWRIGHT_INTEGRITY=sha512-Hu/BMoA1NAdRUuulyvQC0pEqZ4vQbGfn8f7wPXcnqQmM+zct9UliKxsIkLNmz/ku7LElUNqmaiv1TG/aL5ACsw==

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends xz-utils \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz" \
    && echo "${NODE_SHA256}  node-v${NODE_VERSION}-linux-x64.tar.xz" | sha256sum -c - \
    && tar -xJf "node-v${NODE_VERSION}-linux-x64.tar.xz" -C /usr/local --strip-components=1 \
    && rm "node-v${NODE_VERSION}-linux-x64.tar.xz" \
    && observed_integrity="$(npm view "playwright@${PLAYWRIGHT_VERSION}" dist.integrity --registry=https://registry.npmjs.org)" \
    && test "${observed_integrity}" = "${PLAYWRIGHT_INTEGRITY}" \
    && PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 npm install \
      --prefix /opt/kungfu/demo-renderer \
      --omit=dev \
      --ignore-scripts \
      --registry=https://registry.npmjs.org \
      "playwright@${PLAYWRIGHT_VERSION}" \
    && test "$(node --version)" = "v${NODE_VERSION}" \
    && test "$(node -p "require('/opt/kungfu/demo-renderer/node_modules/playwright/package.json').version")" = "${PLAYWRIGHT_VERSION}" \
    && test -x "$(command -v ffmpeg)" \
    && test -x "$(command -v ffprobe)" \
    && test -f /usr/share/doc/fonts-dejavu-core/copyright \
    && mkdir -p /opt/kungfu/demo-renderer/licenses \
    && cp /usr/share/doc/fonts-dejavu-core/copyright /opt/kungfu/demo-renderer/licenses/DejaVu-fonts.copyright \
    && npm cache clean --force

COPY render-demo.mjs /opt/kungfu/demo-renderer/render-demo.mjs
COPY contracts /opt/kungfu/demo-renderer/contracts
COPY font-inventory.json /opt/kungfu/demo-renderer/font-inventory.json
COPY tests /opt/kungfu/demo-renderer/tests

RUN printf '%s\n' '#!/bin/sh' 'exec node /opt/kungfu/demo-renderer/render-demo.mjs "$@"' \
      > /usr/local/bin/demo-renderer \
    && chmod 0555 /usr/local/bin/demo-renderer \
    && chmod 0444 \
      /opt/kungfu/demo-renderer/render-demo.mjs \
      /opt/kungfu/demo-renderer/font-inventory.json \
      /opt/kungfu/demo-renderer/contracts/*.json \
      /opt/kungfu/demo-renderer/tests/fixtures/* \
    && chmod 0555 /opt/kungfu/demo-renderer/tests/smoke.sh \
    && mkdir -p /home/pwuser/.cache /home/pwuser/.config /home/pwuser/.local/share /home/pwuser/.local/state \
    && chown -R pwuser:pwuser /home/pwuser

ENV HOME=/home/pwuser
ENV XDG_CACHE_HOME=/home/pwuser/.cache
ENV XDG_CONFIG_HOME=/home/pwuser/.config
ENV XDG_DATA_HOME=/home/pwuser/.local/share
ENV XDG_STATE_HOME=/home/pwuser/.local/state
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV TZ=UTC
ENV SOURCE_DATE_EPOCH=0

USER pwuser
WORKDIR /work
CMD ["demo-renderer", "--help"]

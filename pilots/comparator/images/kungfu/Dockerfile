FROM ghcr.io/kungfu-systems/build-images/kungfu-verify@sha256:cb6d939d567c129903d6e3ad858e2c09a9475b3b8f81b1aad10279d0e63a920d

ARG KUNGFU_CLI_PACKAGE_SHA256
ARG KUNGFU_CLI_VERSION
ARG KUNGFU_CLI_SOURCE_SHA

USER root

COPY kungfu-episodes-cli-linux-x64.tar.gz /tmp/kungfu-episodes-cli-linux-x64.tar.gz

# This image consumes a CLI package built before the comparator starts. It
# never checks out or compiles Kungfu source inside the Docker environment.
RUN test -n "${KUNGFU_CLI_PACKAGE_SHA256}" \
    && test -n "${KUNGFU_CLI_VERSION}" \
    && printf '%s' "${KUNGFU_CLI_SOURCE_SHA}" | grep -Eq '^[0-9a-f]{40}$' \
    && echo "${KUNGFU_CLI_PACKAGE_SHA256}  /tmp/kungfu-episodes-cli-linux-x64.tar.gz" | sha256sum -c - \
    && mkdir -p /opt/kungfu \
    && tar -xzf /tmp/kungfu-episodes-cli-linux-x64.tar.gz -C /opt/kungfu --strip-components=1 \
    && rm -f /tmp/kungfu-episodes-cli-linux-x64.tar.gz \
    && jq -e \
      '.schema == "kungfu.product.cli/v1" and (.entries.kungfu | type == "string") and (.entries.compatibility | type == "string")' \
      /opt/kungfu/product.json >/dev/null \
    && entry=$(jq -r '.entries.kungfu' /opt/kungfu/product.json) \
    && compatibility=$(jq -r '.entries.compatibility' /opt/kungfu/product.json) \
    && case "$entry" in /*|../*|*/../*|*/..) exit 66 ;; esac \
    && case "$compatibility" in /*|../*|*/../*|*/..) exit 66 ;; esac \
    && test -x "/opt/kungfu/$entry" \
    && test -f "/opt/kungfu/$compatibility" \
    && printf '%s\n' \
      '#!/bin/sh' \
      'set -eu' \
      'entry=$(jq -r '\''.entries.kungfu'\'' /opt/kungfu/product.json)' \
      'exec "/opt/kungfu/$entry" "$@"' \
      >/usr/local/bin/kungfu \
    && chmod +x /usr/local/bin/kungfu \
    && printf '%s\n' "${KUNGFU_CLI_SOURCE_SHA}" >/opt/kungfu/SOURCE_SHA \
    && chmod -R a-w /opt/kungfu

LABEL org.opencontainers.image.version="${KUNGFU_CLI_VERSION}"
LABEL org.opencontainers.image.revision="${KUNGFU_CLI_SOURCE_SHA}"

USER kungfu
WORKDIR /var/lib/kungfu

RUN kungfu -H /tmp/kungfu-package-check agent capabilities --json >/dev/null

HEALTHCHECK --interval=2s --timeout=5s --retries=30 \
  CMD kungfu -H /tmp/kungfu-health agent capabilities --json >/dev/null || exit 1

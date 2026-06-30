ARG BASE_IMAGE=ghcr.io/kungfu-systems/build-images/base-linux:local
FROM ${BASE_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive
ARG FNM_VERSION=1.38.1
ARG FNM_SHA256=b69e5c9a05c1e17e4a7de9a17df14ba430d049f2591af791a6f850a170296069
ARG UV_VERSION=0.11.25
ARG APT_HTTP_PROXY=
ARG PIP_INDEX_URL=

USER root

RUN if [ -n "${APT_HTTP_PROXY}" ]; then \
          printf '%s\n' "Acquire::http::Proxy \"${APT_HTTP_PROXY}\";" > /etc/apt/apt.conf.d/01kungfu-build-cache; \
        fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
      jq \
      python3 \
      python3-pip \
      python3-venv \
      unzip \
    && curl -fsSLo /tmp/fnm.zip "https://github.com/Schniz/fnm/releases/download/v${FNM_VERSION}/fnm-linux.zip" \
    && echo "${FNM_SHA256}  /tmp/fnm.zip" | sha256sum -c - \
    && unzip -q /tmp/fnm.zip -d /usr/local/bin \
    && chmod +x /usr/local/bin/fnm \
    && if [ -n "${PIP_INDEX_URL}" ]; then \
      python3 -m pip install --break-system-packages --no-cache-dir --index-url "${PIP_INDEX_URL}" "uv==${UV_VERSION}"; \
    else \
         python3 -m pip install --break-system-packages --no-cache-dir "uv==${UV_VERSION}"; \
        fi \
        && rm -f /tmp/fnm.zip \
        && rm -f /etc/apt/apt.conf.d/01kungfu-build-cache \
        && rm -rf /var/lib/apt/lists/*

ENV XDG_CACHE_HOME=/home/kungfu/.cache
ENV XDG_DATA_HOME=/home/kungfu/.local/share
ENV UV_LINK_MODE=copy

USER kungfu
WORKDIR /work

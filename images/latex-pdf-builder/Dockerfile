ARG BASE_IMAGE=ghcr.io/kungfu-systems/build-images/node24-pnpm:local
FROM ${BASE_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive
ARG APT_HTTP_PROXY=""

USER root

RUN if [ -n "${APT_HTTP_PROXY}" ]; then \
      printf '%s\n' "Acquire::http::Proxy \"${APT_HTTP_PROXY}\";" > /etc/apt/apt.conf.d/01kungfu-build-cache; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
      biber \
      cm-super \
      ghostscript \
      latexmk \
      lmodern \
      make \
      texlive-bibtex-extra \
      texlive-fonts-recommended \
      texlive-lang-english \
      texlive-latex-extra \
      texlive-latex-recommended \
      texlive-science \
    && rm -f /etc/apt/apt.conf.d/01kungfu-build-cache \
    && rm -rf /var/lib/apt/lists/*

COPY --chown=kungfu:kungfu tests/smoke /opt/kungfu/latex-pdf-builder-smoke

USER kungfu
WORKDIR /work

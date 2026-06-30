ARG BASE_IMAGE=ghcr.io/kungfu-systems/build-images/base-linux:local
FROM ${BASE_IMAGE}

ARG DEBIAN_FRONTEND=noninteractive

USER root

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
      build-essential \
      ccache \
      clang \
      cmake \
      ninja-build \
      pkg-config \
      python3 \
      python3-pip \
      python3-venv \
    && rm -rf /var/lib/apt/lists/*

USER kungfu
WORKDIR /work


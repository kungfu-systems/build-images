#!/bin/bash
set -euo pipefail

test "$(gcc-14 -dumpfullversion)" = "14.2.0"
test "$(g++-14 -dumpfullversion)" = "14.2.0"
test "$(cmake --version | head -n 1)" = "cmake version 3.28.3"
test "$(ninja --version)" = "1.11.1"
test "$(ccache --version | head -n 1)" = "ccache version 4.9.1"
test "$(clang --version | head -n 1)" = "Ubuntu clang version 18.1.3 (1ubuntu1)"
test "$(pkg-config --version)" = "1.8.1"
test "$(python3 --version)" = "Python 3.12.3"
test "$(fnm --version)" = "fnm 1.39.0"
test "$(uv --version | awk '{print $2}')" = "0.11.23"
test "$(rustc --version | awk '{print $2}')" = "1.96.0"
test "$(cargo --version | awk '{print $2}')" = "1.96.0"
test "$COREPACK_NPM_REGISTRY" = "https://registry.npmjs.org"
test "$PNPM_STORE_DIR" = "/home/kungfu/.cache/pnpm/store"
test -z "${KF_LIBWASM_CARGO_REGISTRY:-}"
test "$KUNGFU_BUILD_JOBS" = "12"
bash -n /opt/kungfu-native-source-build/bin/build-kungfu-core
echo "authority=build-reproducibility-only"

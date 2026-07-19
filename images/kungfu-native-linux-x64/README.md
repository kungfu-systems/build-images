# kungfu-native-linux-x64

Pinned `linux/amd64` environment for reproducibly building Kungfu native source.
It extends `kungfu-verify` and adds:

- GCC/G++ 14.2.0, CMake 3.28.3, Ninja 1.11.1, ccache 4.9.1,
  clang 18.1.3, pkg-config 1.8.1, Python 3.12.3, and build-essential;
- verified `fnm` 1.39.0 and `uv` 0.11.23 artifacts;
- Rust/Cargo 1.96.0 installed through verified rustup-init 1.28.2;
- writable HOME-local defaults for pnpm, node-gyp, Corepack, fnm, uv/Python,
  Conan, and Cargo.

The image guarantees build tooling only. It does not grant product,
performance, or host-native qualification authority.

## Consumer Contract

Consumers must use an exact digest, mount a complete writable HOME, and run as
the host uid/gid:

```bash
bash scripts/smoke-kungfu-native-source-build.sh \
  --image ghcr.io/kungfu-systems/build-images/kungfu-native-linux-x64@sha256:DIGEST \
  --source /absolute/path/to/fresh-kungfu-d6fb387-worktree \
  --output /absolute/path/to/evidence
```

The wrapper rejects a non-exact image reference, a source commit other than
`d6fb3879c8f495b6b4e4a1a619ce78358291a6e1`, a dirty source tree, or a nonempty
evidence directory. It uses `SHIFU_NATIVE=0` because that source revision's
alpha.1 launcher asset is absent. It performs the frozen `--no-optional`
install with an explicit pnpm store, seeds the exact libnode platform package,
builds core, verifies the fixture on the host, and emits hashes and cache
inputs.

Optional public mirror inputs are passed as environment variables:

```text
COREPACK_NPM_REGISTRY
NPM_CONFIG_REGISTRY
NODEJS_ORG_MIRROR
UV_PYTHON_INSTALL_MIRROR
KUNGFU_CONAN_REMOTE_URL
KF_LIBWASM_CARGO_REGISTRY
```

`KF_LIBWASM_CARGO_REGISTRY` is empty by default so Cargo uses crates.io
directly. Set it only to a distinct sparse mirror such as
`sparse+https://rsproxy.cn/index/`; configuring crates.io itself as its own
replacement is invalid.
`COREPACK_NPM_REGISTRY` must not end in `/`. Registry values containing embedded
credentials, query parameters, or fragments are rejected so they cannot leak
into the evidence receipt.
`KUNGFU_BUILD_JOBS` defaults to 12 and is also written to Conan's
`tools.build:jobs`, bounding both the main CMake build and dependency recipes.

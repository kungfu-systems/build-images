# Image Contract

Every image is defined by `images/<name>/image.toml`.

Required fields:

```toml
schema = 1
name = "native-linux-x64"
contract_major = 1
platform = "linux-x64"
publish = true

[base]
image = "base-linux"

[runner]
profile = "kungfu-build-v4-linux-x64"
self_hosted_required = false

[build]
context = "."
dockerfile = "Dockerfile"
test_commands = [
  "python3 --version",
]
```

## Contract Identity

An image contract is identified by:

- image name;
- `contract_major`;
- platform;
- documented runner boundary;
- published digest.

Breaking changes should create a new `contract_major` or a new image name. A
release must not silently change the meaning of an existing image contract.

## Parent Images

Child images must reference parent images from this repository by manifest name.
Release automation should resolve parent image digests before building child
images.

The first graph is intentionally shallow:

```text
base-linux
  -> node24-pnpm
  -> native-linux-x64
```

## Test Commands

`test_commands` are cheap smoke commands that prove the image contract is
present. They are not consumer release builds.

Consumer repositories own their package-specific build commands.


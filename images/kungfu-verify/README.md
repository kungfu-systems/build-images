# kungfu-verify

Lightweight Kungfu verification image for CI jobs that need the Buildchain
entry tools but do not need native compilation.

It fixes the moving runner surface for `kungfu-code sync` and similar verify or
publish preparation jobs:

- fnm
- uv
- Python 3
- jq
- git, curl, CA certificates, locale, and the `kungfu` user inherited from
  `base-linux`

Native build tools such as CMake, Ninja, clang, ccache, and Conan belong in a
heavier image layered above this one.


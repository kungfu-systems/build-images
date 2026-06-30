# native-linux-x64

Common native Linux x64 build image for Kungfu consumers.

This image derives from `base-linux` and provides a baseline C/C++ build
toolchain, CMake, Ninja, Python, and ccache. Scenario-specific native images
should derive from this image instead of expanding `base-linux` directly.


# aeron-native-kit

Immutable Aeron 1.52.2 qualification kit for `linux/amd64`. The image pins the
Temurin JDK and JRE, verifies the Aeron JAR checksum, and compiles the bounded,
noninteractive qualification harness during the image build.

The harness exposes a live Driver/Archive health probe and marker-bound record
and replay commands. Formal receipts bind every frame to a caller-supplied
SHA-256 marker and fail on missing, duplicate, reordered, or mismatched data.
The media term, Archive segment, threading modes, idle strategy, sparse-file
policy, control channels, and sync levels are fixed in `kit-manifest.json` and
echoed by the live server receipt.

This image is a distribution artifact only. The native qualification runner
extracts the JRE and `/opt/aeron-native-kit` from an exact accepted image
digest, removes the temporary extraction container, and verifies every file
before starting host-native measurement. Running this image as a container
never grants native performance authority.

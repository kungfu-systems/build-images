# aeron-native-kit

Immutable Aeron 1.52.2 qualification kit for `linux/amd64`. The image pins the
Temurin JDK and JRE, verifies the Aeron JAR checksum, and compiles the bounded,
noninteractive qualification harness during the image build.

This image is a distribution artifact only. The native qualification runner
extracts the JRE and `/opt/aeron-native-kit` from an exact accepted image
digest, removes the temporary extraction container, and verifies every file
before starting host-native measurement. Running this image as a container
never grants native performance authority.

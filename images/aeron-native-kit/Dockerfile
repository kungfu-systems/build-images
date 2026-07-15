FROM eclipse-temurin:21.0.11_10-jdk-jammy@sha256:a5418a1fcf440bb273e1db3bce5b0794eb78bfc9d044ba740de76dcbe6075f50 AS build

ADD --checksum=sha256:f02b1cc8660ee24235f41000f3b4a7af90730cc467519f2344672208e7195a6f \
    https://repo.maven.apache.org/maven2/io/aeron/aeron-all/1.52.2/aeron-all-1.52.2.jar \
    /opt/build/aeron-all.jar
COPY src /opt/build/src
RUN mkdir -p /opt/build/classes && \
    javac --release 21 -cp /opt/build/aeron-all.jar \
      -d /opt/build/classes /opt/build/src/io/kungfu/aeron/QualificationHarness.java && \
    jar --create --file /opt/build/aeron-qualification-harness.jar \
      --main-class io.kungfu.aeron.QualificationHarness -C /opt/build/classes .

FROM eclipse-temurin:21.0.11_10-jre-jammy@sha256:d63bd8d9b171999cbed8576f2c76e874dd4856791a358536e5c4d407e77edc13

COPY --from=build /opt/build/aeron-all.jar /opt/aeron-native-kit/lib/aeron-all.jar
COPY --from=build /opt/build/aeron-qualification-harness.jar /opt/aeron-native-kit/lib/aeron-qualification-harness.jar
COPY bin/aeron-native-harness /opt/aeron-native-kit/bin/aeron-native-harness
COPY kit-manifest.json /opt/aeron-native-kit/kit-manifest.json

RUN chmod 0755 /opt/aeron-native-kit/bin/aeron-native-harness

ENV AERON_NATIVE_KIT_JAVA=/opt/java/openjdk/bin/java
ENTRYPOINT ["/opt/aeron-native-kit/bin/aeron-native-harness"]
CMD ["version"]

LABEL org.opencontainers.image.source="https://github.com/kungfu-systems/build-images"
LABEL org.opencontainers.image.version="aeron-1.52.2"
LABEL io.kungfu.aeron.version="1.52.2"
LABEL io.kungfu.aeron.jar.sha256="f02b1cc8660ee24235f41000f3b4a7af90730cc467519f2344672208e7195a6f"
LABEL io.kungfu.native-kit.authority="distribution-only"

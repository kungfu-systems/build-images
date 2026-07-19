ARG BASE_IMAGE=ghcr.io/kungfu-systems/build-images/kungfu-verify:local

FROM eclipse-temurin:21.0.11_10-jdk-jammy@sha256:a5418a1fcf440bb273e1db3bce5b0794eb78bfc9d044ba740de76dcbe6075f50 AS aeron-driver-build

ADD --checksum=sha256:f02b1cc8660ee24235f41000f3b4a7af90730cc467519f2344672208e7195a6f \
    https://repo.maven.apache.org/maven2/io/aeron/aeron-all/1.52.2/aeron-all-1.52.2.jar \
    /opt/build/aeron-all.jar
COPY opt/formal-performance/drivers/aeron/src /opt/build/src
RUN mkdir -p /opt/build/classes \
    && javac --release 21 -cp /opt/build/aeron-all.jar \
      -d /opt/build/classes \
      /opt/build/src/io/kungfu/aeron/FormalPerformanceHarness.java \
    && jar --create \
      --file /opt/build/formal-performance-aeron.jar \
      --main-class io.kungfu.aeron.FormalPerformanceHarness \
      -C /opt/build/classes .

FROM ${BASE_IMAGE}

USER root

COPY opt/formal-performance /opt/formal-performance
COPY --from=aeron-driver-build /opt/build/formal-performance-aeron.jar \
  /opt/formal-performance/drivers/aeron/formal-performance-aeron.jar
RUN chmod 0755 \
      /opt/formal-performance/bin/formal-performance \
      /opt/formal-performance/bin/formal-performance-provider \
      /opt/formal-performance/bin/compile-kungfu-formal-driver \
      /opt/formal-performance/tests/smoke.sh \
    && ln -s /opt/formal-performance/bin/formal-performance /usr/local/bin/formal-performance

USER kungfu
WORKDIR /work

LABEL io.kungfu.formal-performance.evidence-class="symmetric-formal-performance"
LABEL io.kungfu.formal-performance.winner-authority="false"
LABEL io.kungfu.formal-performance.runner-mode="host-extracted"

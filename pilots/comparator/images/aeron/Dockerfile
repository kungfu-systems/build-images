FROM eclipse-temurin:21.0.11_10-jre-jammy@sha256:d63bd8d9b171999cbed8576f2c76e874dd4856791a358536e5c4d407e77edc13

ADD --checksum=sha256:f02b1cc8660ee24235f41000f3b4a7af90730cc467519f2344672208e7195a6f \
    https://repo.maven.apache.org/maven2/io/aeron/aeron-all/1.52.2/aeron-all-1.52.2.jar \
    /opt/aeron/aeron-all.jar

RUN mkdir -p /var/lib/aeron/archive /var/lib/aeron/restore

WORKDIR /var/lib/aeron

LABEL org.opencontainers.image.source="https://github.com/aeron-io/aeron" \
      org.opencontainers.image.version="1.52.2" \
      org.opencontainers.image.description="Unscored Aeron comparator pilot image"

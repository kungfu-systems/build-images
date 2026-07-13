FROM ghcr.io/kungfu-systems/build-images/kungfu-verify@sha256:cb6d939d567c129903d6e3ad858e2c09a9475b3b8f81b1aad10279d0e63a920d

ARG KUNGFU_ARTIFACT_URL
ARG KUNGFU_ARTIFACT_SHA256

RUN test -n "${KUNGFU_ARTIFACT_URL}" \
    && test "${KUNGFU_ARTIFACT_URL}" != "BLOCKED_NO_FORMAL_ARTIFACT" \
    && test -n "${KUNGFU_ARTIFACT_SHA256}" \
    && test "${KUNGFU_ARTIFACT_SHA256}" != "BLOCKED_NO_FORMAL_ARTIFACT" \
    || { echo >&2 "A formal Kungfu product artifact URL and SHA-256 are required"; exit 64; }

# The install and smoke contract intentionally remains fail-closed until the
# formal product artifact publishes its archive layout and release evidence.
RUN echo >&2 "Kungfu artifact layout is not yet published; installer remains blocked" \
    && exit 65

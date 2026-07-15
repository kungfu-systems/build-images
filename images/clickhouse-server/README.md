# clickhouse-server

Build-images-owned mirror of ClickHouse `26.3.10.60-lts` for reproducible
comparator qualification. The Dockerfile pins the upstream Docker Hub manifest
by digest; released GHCR manifests retain Buildchain source and material labels.

Consumers must use the accepted GHCR digest from `images.lock.json`. Mutable
tags and tester-created mirror images are outside the qualification contract.

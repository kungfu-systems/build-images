#!/bin/sh
set -eu

latexmk -version
biber --version
pnpm --version

work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

cp -R /opt/kungfu/latex-pdf-builder-smoke "$work_dir/smoke"
cd "$work_dir/smoke"
pnpm run pdf
test -s _build/main.pdf
sha256sum _build/main.pdf

#!/bin/sh
set -eu

if [ "$#" -ne 1 ]; then
  echo "usage: $0 <mltd-asset-version>" >&2
  exit 2
fi

VERSION="$1"
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
cd "$ROOT"

docker compose --profile tools run --rm --no-deps mltd-asset-tools   tools/archive_controller.py --root /data activate --version "$VERSION"

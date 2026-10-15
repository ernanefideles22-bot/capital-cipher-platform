#!/bin/sh
set -eu

DATA_ROOT="${DATA_LAKE_ROOT:-/var/lib/capital-cipher/data-lake}"
PERSIST_ROOT="$(dirname "$DATA_ROOT")"

mkdir -p "$DATA_ROOT"
chown -R 10001:10001 "$PERSIST_ROOT"

exec gosu capitalcipher "$@"

#!/bin/sh
# Run from a prepared release directory; no sudo, boot changes or CPU fallback.
set -eu
RELEASE=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
VENDOR_LIB=${POWERSCOPE_VENDOR_LIB:-/home/uisrc/powerscope-dr1-tests/20261031-sdk-v3/libs/armnn_lib/lib}
STORE=${POWERSCOPE_STORE:-/home/uisrc/powerscope-dr1-results/ps-replay}
REVISION=$(cat "$RELEASE/source-revision.txt" 2>/dev/null || printf unversioned)
exec python3 "$RELEASE/ps/app.py" \
  --assets "$RELEASE/assets/manifest.json" \
  --worker "$RELEASE/bin/ps_npu_worker" --vendor-lib "$VENDOR_LIB" \
  --store "$STORE" --revision "$REVISION" \
  --mmio-lib "$RELEASE/bin/libpowerscope_mmio.so" "$@"

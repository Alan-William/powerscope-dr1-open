#!/bin/sh
set -eu
RELEASE=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
export POWERSCOPE_STORE=/home/uisrc/powerscope-dr1-results/production-sfaug-r1-events
exec sh "$RELEASE/board/deploy/run_ps_replay.sh" \
  --axi-config /run/powerscope/axi-uio.json \
  --waveform-config /run/powerscope/waveform-uio.json \
  --recover-interrupted-waveform --max-frames 100000

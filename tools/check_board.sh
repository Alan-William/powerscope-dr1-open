#!/bin/sh
# Read-only inventory. Run after the user connects the board.
set -eu
uname -a
printf '\nDevice tree compatible:\n'
if [ -r /proc/device-tree/compatible ]; then tr '\000' '\n' </proc/device-tree/compatible; fi
printf '\nNPU/CMA device nodes:\n'
find /dev -maxdepth 1 \( -iname '*npu*' -o -iname '*cma*' \) -print
printf '\nModules:\n'
lsmod | grep -Ei 'npu|cma' || true
printf '\nDevice tree NPU nodes:\n'
# /proc/device-tree is normally a symlink; find without -L misses its children.
find -L /proc/device-tree -iname '*npu*' -print 2>/dev/null || true
printf '\nNPU platform driver bindings:\n'
for device in /sys/bus/platform/devices/*npu*; do
    [ -e "$device" ] || continue
    printf '%s: ' "$device"
    if [ -L "$device/driver" ]; then readlink "$device/driver"; else printf 'UNBOUND\n'; fi
done
printf '\nCMA reserved memory (not proof of /dev/cma_mem driver):\n'
grep -i cma /proc/meminfo || true
printf '\nArm NN libraries:\n'
ldconfig -p 2>/dev/null | grep -Ei 'armnn|alnpu' || true

#!/bin/sh
# Run under the LinuxFB system service, or from an existing X11 desktop.
# The HDMI-capable image must have registered /dev/fb0 first.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)"
export DISPLAY="${DISPLAY:-:0}"
export QT_X11_NO_MITSHM=1
export QT_AUTO_SCREEN_SCALE_FACTOR=0
# The previously tested framebuffer is 1920x1080; the touchscreen scales it.
# Set QT_SCALE_FACTOR=1 for a future native 1024x600 mode.
export QT_SCALE_FACTOR="${QT_SCALE_FACTOR:-1.5}"
exec /usr/bin/python3 -m board_display --fullscreen

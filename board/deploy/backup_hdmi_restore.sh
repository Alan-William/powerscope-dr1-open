#!/bin/sh
# Run as root on the connected DR1 before deploying the display-profile image.
set -eu
test "$(id -u)" = 0
dest=/var/backups/powerscope-hdmi-20261104/pre-restore
test ! -e "$dest"
test "$(findmnt -n -o SOURCE --target /boot/board)" = /dev/mmcblk0p1
printf '%s\n' \
  '0d0bd56ab9de02409a5f5b89d63b74ad13e0aa16a84ebfb18266f23330933fbe  /boot/board/BOOT.bin' \
  '6dfa8303f5b06f101414adfae5597edd9e37002b15ee5476d2ae87e62f4dbf40  /boot/board/system.dtb' | sha256sum -c -
umask 077
mkdir -p "$dest/boot" "$dest/config"
cp -a /boot/board/BOOT.bin /boot/board/system.dtb "$dest/boot/"
cp -a /usr/local/lib/powerscope/bind_headless_uio.py "$dest/config/"
cp -a /etc/systemd/system/powerscope-ps.service /etc/systemd/system/powerscope-uio.service "$dest/config/"
cp -a /etc/X11/xorg.conf "$dest/config/"
readlink -f /opt/powerscope/current > "$dest/current-release.txt"
systemctl cat powerscope-ps powerscope-uio lightdm > "$dest/services.txt"
systemctl is-enabled lightdm > "$dest/lightdm-enabled.txt" || true
sha256sum "$dest/boot/BOOT.bin" "$dest/boot/system.dtb" > "$dest/SHA256SUMS"
sync
cat "$dest/SHA256SUMS"
printf 'HDMI_BACKUP_READY %s\n' "$dest"

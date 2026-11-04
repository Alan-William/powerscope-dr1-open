#!/bin/sh
# Reviewed for this board's existing SD Ubuntu boot partition only.
# Run interactively with sudo; does not store passwords or reboot automatically.
set -eu
[ "$(id -u)" = 0 ] || { echo 'Run with sudo in an interactive terminal'; exit 1; }
stage=/home/uisrc/powerscope-dr1-tests/20261031-npu-deploy
backup=/var/backups/powerscope-npu-20261031
boot=/boot/board
rules=/etc/udev/rules.d/70-powerscope-npu.rules
[ "$(findmnt -n -o SOURCE --target "$boot")" = /dev/mmcblk0p1 ] || { echo 'Unexpected boot device'; exit 1; }
[ "$(findmnt -n -o FSTYPE --target "$boot")" = vfat ] || exit 1
check_old() {
    (cd "$boot"; sha256sum -c <<'HASHES'
60c2fff36781584a63597df420a441cbe73fb302c2feec1c147d2002f75a1190  BOOT.bin
a414efb6b8d2fb74cd94c7e5fd0f83cd91ba9945490dfd30f36be61fdc3e6656  boot.scr
e521c7288692714f3ba069561e5e0fee62f9898a4241115a7469f81b8eca18b5  system.dtb
07fb0ebf237f4b55e3206b84f8e08aaf3cc678648c179911c836341073fa7892  uImage.lz4
HASHES
    )
}
check_new() {
    (cd "$1"; sha256sum -c <<'HASHES'
567e14124fa17137e964ebcadd4680d8349058beacd4070377ba0ac3fc8c2585  BOOT.bin
e1ddd7405245d7b9f644c64066bda64369edbac0d8e8676564261d9cfc22b979  system.dtb
b570c65a8deef13bbf7770038a779a2b94c97000afb564896ccdd3e6bba7fab9  uImage.lz4
HASHES
    )
}
case "${1:-}" in
install)
    check_old
    check_new "$stage/vendor"
    [ ! -e "$backup" ] || { echo 'Backup already exists: inspect prior attempt before retry'; exit 1; }
    [ ! -e "$rules" ] || { echo 'Existing device rules: inspect before replacement'; exit 1; }
    mkdir -m 700 "$backup"
    tar -cf "$backup/boot-before.tar" -C "$boot" .
    (cd "$backup"; sha256sum boot-before.tar > backup.sha256)
    # Validate saved file content, independent of tar header metadata.
    mkdir "$backup/verify"
    tar -xf "$backup/boot-before.tar" -C "$backup/verify"
    saved_boot=$boot; boot=$backup/verify; check_old; boot=$saved_boot
    cp "$stage/backup/boot-files.sha256" "$backup/original-files.sha256"
    # Stage all three files before replacing any active file. Paired replacement
    # is not power-loss atomic: keep power connected until completion.
    for name in uImage.lz4 system.dtb BOOT.bin; do
        cp "$stage/vendor/$name" "$boot/$name.powerscope-new"
        cmp "$stage/vendor/$name" "$boot/$name.powerscope-new"
    done
    sync
    for name in uImage.lz4 system.dtb BOOT.bin; do mv "$boot/$name.powerscope-new" "$boot/$name"; done
    check_new "$boot"
    (cd "$boot"; echo 'a414efb6b8d2fb74cd94c7e5fd0f83cd91ba9945490dfd30f36be61fdc3e6656  boot.scr' | sha256sum -c -)
    # Grant only the task account access to the three vendor character devices.
    # The Ubuntu root filesystem, SSH credentials, sudoers and boot.scr are preserved.
    cat > "$rules" <<'RULES'
KERNEL=="hard_npu", OWNER="uisrc", GROUP="uisrc", MODE="0600"
KERNEL=="soft_npu", OWNER="uisrc", GROUP="uisrc", MODE="0600"
KERNEL=="cma_mem", OWNER="uisrc", GROUP="uisrc", MODE="0600"
RULES
    chmod 644 "$rules"
    sync
    echo 'Installed vendor BOOT/kernel/device tree; preserved Ubuntu boot.scr.'
    echo "Root-owned rollback backup: $backup"
    echo 'Reboot only after the UART recovery connection has been verified.'
    ;;
restore)
    [ -f "$backup/boot-before.tar" ] || exit 1
    (cd "$backup"; sha256sum -c backup.sha256)
    tar -xf "$backup/boot-before.tar" -C "$boot"
    check_old
    # Remove only the device rule written by this installer, if unchanged.
    if [ -f "$rules" ]; then
        expected='KERNEL=="hard_npu", OWNER="uisrc", GROUP="uisrc", MODE="0600"
KERNEL=="soft_npu", OWNER="uisrc", GROUP="uisrc", MODE="0600"
KERNEL=="cma_mem", OWNER="uisrc", GROUP="uisrc", MODE="0600"'
        [ "$(cat "$rules")" = "$expected" ] || { echo 'Device rules changed: leave them for review'; exit 1; }
        rm -- "$rules"
    fi
    sync
    echo 'Original boot files restored and verified; reboot required to activate.'
    ;;
*) echo "Usage: $0 install|restore"; exit 2 ;;
esac

#!/bin/sh
# Authorized 236df82 headless candidate; exact SD image only, no automatic reboot.
set -eu
[ "$(id -u)" = 0 ] || { echo 'Root console required'; exit 1; }
stage=/home/uisrc/powerscope-dr1-tests/20261101-pl-headless-236df82
backup=/var/backups/powerscope-headless-236df82-20261101
boot=/boot/board
[ "$(findmnt -n -o SOURCE --target "$boot")" = /dev/mmcblk0p1 ] || exit 1
[ "$(findmnt -n -o FSTYPE --target "$boot")" = vfat ] || exit 1
check_old() {
    (cd "$1"; sha256sum -c <<'HASHES'
567e14124fa17137e964ebcadd4680d8349058beacd4070377ba0ac3fc8c2585  BOOT.bin
e1ddd7405245d7b9f644c64066bda64369edbac0d8e8676564261d9cfc22b979  system.dtb
b570c65a8deef13bbf7770038a779a2b94c97000afb564896ccdd3e6bba7fab9  uImage.lz4
a414efb6b8d2fb74cd94c7e5fd0f83cd91ba9945490dfd30f36be61fdc3e6656  boot.scr
HASHES
    )
}
check_pair() {
    (cd "$1"; sha256sum -c <<'HASHES'
0d0bd56ab9de02409a5f5b89d63b74ad13e0aa16a84ebfb18266f23330933fbe  BOOT.bin
6dfa8303f5b06f101414adfae5597edd9e37002b15ee5476d2ae87e62f4dbf40  system.dtb
HASHES
    )
}
case "${1:-}" in
install)
    check_old "$boot"
    check_pair "$stage/candidate"
    [ ! -e "$backup" ] || { echo 'Existing backup: inspect prior attempt'; exit 1; }
    for name in BOOT.bin system.dtb; do
        [ ! -e "$boot/$name.powerscope236df82-new" ] || exit 1
    done
    # Do not replace boot files while the known NPU worker is still active.
    if pgrep -x ps_npu_worker >/dev/null; then echo 'Stop NPU worker before installing'; exit 1; fi
    mkdir -m 700 "$backup"
    mkdir -m 700 "$backup/original"
    for name in BOOT.bin system.dtb uImage.lz4 boot.scr; do
        cp -p "$boot/$name" "$backup/original/$name"
    done
    check_old "$backup/original"
    cp "$stage/candidate/manifest.json" "$backup/candidate-manifest.json"
    cp "$stage/install_headless.sh" "$backup/install_headless.sh"
    # Prepare and compare both files before changing either active filename.
    for name in BOOT.bin system.dtb; do
        cp "$stage/candidate/$name" "$boot/$name.powerscope236df82-new"
        cmp "$stage/candidate/$name" "$boot/$name.powerscope236df82-new"
    done
    sync
    for name in system.dtb BOOT.bin; do
        mv "$boot/$name.powerscope236df82-new" "$boot/$name"
    done
    check_pair "$boot"
    cmp "$backup/original/uImage.lz4" "$boot/uImage.lz4"
    cmp "$backup/original/boot.scr" "$boot/boot.scr"
    sync
    printf 'HEADLESS_INSTALL_OK\nBackup: %s\nReboot still required.\n' "$backup"
    ;;
restore)
    check_old "$backup/original"
    for name in BOOT.bin system.dtb; do
        [ ! -e "$boot/$name.powerscope236df82-restore" ] || exit 1
        cp "$backup/original/$name" "$boot/$name.powerscope236df82-restore"
        cmp "$backup/original/$name" "$boot/$name.powerscope236df82-restore"
    done
    sync
    for name in system.dtb BOOT.bin; do
        mv "$boot/$name.powerscope236df82-restore" "$boot/$name"
    done
    check_old "$boot"
    sync
    echo 'HEADLESS_RESTORE_OK; reboot still required.'
    ;;
*) echo "Usage: $0 install|restore"; exit 2 ;;
esac

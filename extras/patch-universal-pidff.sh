#!/bin/sh
# Adds the PXN V10 Ultra (36e6:410b) to the universal-pidff DKMS driver, rebuilds it for every
# installed kernel and reloads it. Run with: sudo sh patch-universal-pidff.sh
set -e
S=$(ls -d /usr/src/universal-pidff-* | head -1)
V=${S#/usr/src/universal-pidff-}
grep -q 'PXN_V10_ULTRA' $S/hid-ids.h || sed -i '/USB_DEVICE_ID_PXN_VD6/a #define USB_DEVICE_ID_PXN_V10_ULTRA\t0x410b' $S/hid-ids.h
grep -q 'PXN_V10_ULTRA' $S/hid-universal-pidff.c || sed -i '/USB_DEVICE_ID_PXN_VD6),/{n;a\	{ HID_USB_DEVICE(USB_VENDOR_ID_PXN, USB_DEVICE_ID_PXN_V10_ULTRA),\n\t  .driver_data = HID_PIDFF_QUIRK_PERIODIC_SINE_ONLY },
}' $S/hid-universal-pidff.c
grep -n -A1 'V10_ULTRA' $S/hid-ids.h $S/hid-universal-pidff.c
for k in $(ls /lib/modules); do dkms build -m universal-pidff -v $V -k $k --force && dkms install -m universal-pidff -v $V -k $k --force; done
modprobe -r hid_universal_pidff 2>/dev/null || true
modprobe hid_universal_pidff
echo "--- re-plug the wheel (or reboot) now ---"

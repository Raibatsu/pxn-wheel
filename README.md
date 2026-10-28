# pxn-wheel

A small Linux GUI for the **PXN V10 Ultra** racing wheel: live input view, calibration, button
remapping, and force-feedback testing.

Tested on the latest CachyOS with a PXN V10 Ultra (USB ID `36e6:410b`); everything works.

## What it does

- **Live view** – rotating wheel, all analog axes, and every button
- **Steering** – set center, capture lock-to-lock, limit rotation (in degrees), deadzone, linearity, invert
- **Pedals and axes** – per-axis calibration, naming, invert, deadzones, response curve, axis reassignment
- **Buttons** – detect a button by pressing it, remap or disable any button
- **Force feedback** – overall strength plus test effects (pull, spring, damper, friction, vibration)
- **Profiles** – export / import / reset settings

## How it works

The kernel driver gives no way to write calibration to the wheel, so it is done in software. A small
background service grabs the real wheel, applies your settings, and exposes a virtual device named
**"PXN V10 Ultra (pxn-wheel)"**. Games use that virtual device. Force-feedback effects from the game
are forwarded to the real wheel. While the service runs, the real wheel's device files are hidden
from normal users on purpose, so a game can't pick the uncalibrated one; they are restored when the
service stops.

## Requirements

- A PXN V10 Ultra
- **[universal-pidff](https://github.com/JacKeTUs/universal-pidff) installed** – this is required.
  Without it the wheel falls back to the generic HID driver and force feedback is unreliable
  (on CachyOS/Arch: `universal-pidff-dkms-git` from the AUR). The V10 Ultra's ID may not be in
  your version of the driver yet – see step 2.
- Python 3, `python-evdev`, `PySide6`
- `polkit` (for the GUI's service buttons), `systemd`

## Install

**1. Install dependencies**

```sh
sudo pacman -S --needed python-evdev pyside6 polkit
# kernel headers for your kernel(s), e.g. linux-cachyos-headers, then:
yay -S universal-pidff-dkms-git
```

**2. Make sure the driver knows the V10 Ultra**

```sh
modinfo hid_universal_pidff | grep -i 410B
```

If that prints a line, skip to step 3. Otherwise add the ID (this edits the DKMS source, rebuilds the
module for every installed kernel and reloads it):

```sh
sudo sh extras/patch-universal-pidff.sh
```

Re-run it after the driver package updates, until the ID is added upstream. Then unplug and replug
the wheel. `dmesg | grep pidff` should show `Universal pidff driver loaded successfully!`.

**3. Allow your user to access the wheel**

```sh
sudo install -m644 extras/70-pxn-v10-ultra.rules /etc/udev/rules.d/
sudo udevadm control --reload && sudo udevadm trigger
```

Unplug and replug the wheel.

**4. Get the app**

```sh
git clone https://github.com/Raibatsu/pxn-wheel.git
cd pxn-wheel
./pxn-wheel
```

Optional launcher entry:

```sh
cat > ~/.local/share/applications/pxn-wheel.desktop <<EOF
[Desktop Entry]
Type=Application
Name=PXN Wheel Manager
Comment=Calibrate and remap the PXN V10 Ultra racing wheel
Exec=$PWD/pxn-wheel
Icon=input-gaming
Categories=Settings;HardwareSettings;Game;
EOF
```

**5. Install the background service**

In the app, open **Service and profiles** and click **Install & start at boot** (asks for your
password), or from a terminal:

```sh
sudo sh install-service.sh install
```

This copies the code to `/usr/local/lib/pxn-wheel` (root-owned, so the root service never runs code
from a folder you can edit), installs `pxn-wheeld.service`, and starts it.

**6. Restart Steam, then your game**

Steam keeps the raw wheel open until it restarts. After restarting it, select
**"PXN V10 Ultra (pxn-wheel)"** in your game. If a game still shows the wheel as a plain gamepad,
set the game's Steam Input override to *Disable Steam Input*.

## Usage

Run `./pxn-wheel`. Changes save automatically and the service picks them up live. After updating the
app, run `sudo sh install-service.sh install` again to refresh the installed copy.

## Uninstall

```sh
sudo sh /usr/local/lib/pxn-wheel/service.sh uninstall
```

## Limitations

- Calibration and the rotation limit are software-only. The real wheel still turns to its full lock,
  and a game's force-feedback end-stops stay at the original range.
- Games that read the wheel directly through `hidraw` instead of the input device will not see the
  virtual wheel's calibration.
- Only the PXN V10 Ultra is tested.

## License

MIT, see `LICENSE`.

---

*This project was heavily influenced by AI.*

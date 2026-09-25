#!/bin/bash
# Barrett boot splash: white screen, Barrett logo, orange spinner.
#
#   ./install-splash.sh          install + make it the boot splash (~1-2 min)
#   ./install-splash.sh --undo   go back to the previous splash
#
# No kernel/boot-config changes: the Touch Display 2's raw framebuffer is
# portrait and shows up rotated 90 deg CW, so the theme itself draws the logo
# pre-rotated 90 deg CCW (and upright on a landscape screen, e.g. HDMI).
# Images come from sandbox/assets/splash (make-splash.py).
# A broken theme can't stop the Pi booting -- Plymouth falls back to text.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/assets/splash"
NAME=barrett-pendulum
DIR=/usr/share/plymouth/themes/$NAME
PREV=/etc/barrett-splash.previous

if [ "${1:-}" = "--undo" ]; then
    prev=$(cat "$PREV" 2>/dev/null)
    if [ -n "$prev" ] && [ -e "$prev" ]; then
        sudo update-alternatives --set default.plymouth "$prev"
    else
        sudo update-alternatives --auto default.plymouth
    fi
    sudo update-alternatives --remove default.plymouth "$DIR/$NAME.plymouth" 2>/dev/null
    echo "Rebuilding the boot image (1-2 min)..."
    sudo update-initramfs -u && echo "OK  previous splash restored -- reboot to see it"
    exit 0
fi

for f in logo.png logo-ccw.png spinner.png; do
    [ -e "$SRC/$f" ] || { echo "missing $SRC/$f -- run: python3 $HERE/make-splash.py"; exit 1; }
done

# Plymouth's script plugin (Ubuntu keeps some themes/plugins in plymouth-themes)
if ! ls /usr/lib/*/plymouth/script.so /usr/lib/plymouth/script.so >/dev/null 2>&1; then
    sudo apt-get install -y plymouth-themes || { echo "XX couldn't install plymouth-themes"; exit 1; }
fi

sudo mkdir -p "$DIR"
sudo cp "$SRC/logo.png" "$SRC/logo-ccw.png" "$SRC/spinner.png" "$DIR/"

sudo tee "$DIR/$NAME.plymouth" >/dev/null <<EOF
[Plymouth Theme]
Name=Barrett Pendulum
Description=Barrett logo with an orange spinner (Touch Display 2 aware)
ModuleName=script

[script]
ImageDir=$DIR
ScriptFile=$DIR/$NAME.script
EOF

sudo tee "$DIR/$NAME.script" >/dev/null <<'EOF'
Window.SetBackgroundTopColor(1, 1, 1);
Window.SetBackgroundBottomColor(1, 1, 1);

W = Window.GetWidth();
H = Window.GetHeight();
S = Math.Min(W, H);

if (W < H) {
    logo_image = Image("logo-ccw.png");
    long_side = H * 0.47;
    logo_image = logo_image.Scale(long_side * logo_image.GetWidth() / logo_image.GetHeight(), long_side);
    logo_cx = W / 2 - W * 0.08;
    logo_cy = H / 2;
    spin_cx = W / 2 + W * 0.22;
    spin_cy = H / 2;
} else {
    logo_image = Image("logo.png");
    long_side = W * 0.47;
    logo_image = logo_image.Scale(long_side, long_side * logo_image.GetHeight() / logo_image.GetWidth());
    logo_cx = W / 2;
    logo_cy = H / 2 - H * 0.08;
    spin_cx = W / 2;
    spin_cy = H / 2 + H * 0.22;
}

logo_sprite = Sprite(logo_image);
logo_sprite.SetX(logo_cx - logo_image.GetWidth() / 2);
logo_sprite.SetY(logo_cy - logo_image.GetHeight() / 2);

spin_base = Image("spinner.png");
spin_size = S * 0.09;
spin_base = spin_base.Scale(spin_size, spin_size);
spin_sprite = Sprite(spin_base);
spin_angle = 0;

fun refresh_callback() {
    spin_angle = spin_angle + 0.12;
    img = spin_base.Rotate(spin_angle);
    spin_sprite.SetImage(img);
    spin_sprite.SetX(spin_cx - img.GetWidth() / 2);
    spin_sprite.SetY(spin_cy - img.GetHeight() / 2);
}
Plymouth.SetRefreshFunction(refresh_callback);
EOF

# remember the current splash for --undo, then switch
if [ ! -e "$PREV" ]; then
    update-alternatives --query default.plymouth 2>/dev/null | awk '/^Value:/{print $2}' | sudo tee "$PREV" >/dev/null
fi
sudo update-alternatives --install /usr/share/plymouth/themes/default.plymouth default.plymouth \
    "$DIR/$NAME.plymouth" 200
sudo update-alternatives --set default.plymouth "$DIR/$NAME.plymouth"

echo "Rebuilding the boot image so the splash is used at boot (1-2 min)..."
sudo update-initramfs -u || { echo "XX update-initramfs failed -- run $0 --undo"; exit 1; }
echo
echo "OK  Barrett splash installed. Reboot to see it.  Undo: $0 --undo"

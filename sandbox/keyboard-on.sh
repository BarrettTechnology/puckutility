#!/bin/bash
# Turn GNOME's on-screen keyboard on (it appears when you tap a text field).
#   ~/puckutility/sandbox/keyboard-on.sh
# Off again:  gsettings set org.gnome.desktop.a11y.applications screen-keyboard-enabled false
gsettings set org.gnome.desktop.a11y.applications screen-keyboard-enabled true \
    && echo "OK  on-screen keyboard on -- tap a text field to see it"

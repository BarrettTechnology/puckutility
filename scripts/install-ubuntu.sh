#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
# Run setup socketcan
sudo ./setup-socketcan.sh
# Install Application on Ubuntu
sudo rm -rf /usr/local/bin/PuckUtilityApp
sudo rm -f /usr/share/applications/PuckUtilityApp.desktop
sudo rm -f ~/Desktop/PuckUtilityApp.desktop
sudo cp -r "$SCRIPT_DIR" /usr/local/bin/PuckUtilityApp
sudo cp images/BarrettIcon.png /usr/share/pixmaps/PuckUtilityApp.png
sudo cp PuckUtilityApp.desktop ~/.local/share/applications/
sudo cp PuckUtilityApp.desktop /usr/share/applications/
cp PuckUtilityApp.desktop ~/Desktop
gio set ~/Desktop/PuckUtilityApp.desktop metadata::trusted true
chmod a+x ~/Desktop/PuckUtilityApp.desktop


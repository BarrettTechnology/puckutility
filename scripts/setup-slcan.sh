#!/bin/bash
# run to setup slcan for can0 interface (USBTin config)
sudo cp slcan_add.sh /usr/local/bin/ && sudo chmod +x /usr/local/bin/slcan_add.sh
sudo cp slcan_remove.sh /usr/local/bin/ && sudo chmod +x /usr/local/bin/slcan_remove.sh
sudo cp 90-slcan.rules /etc/udev/rules.d
sudo cp slcan@.service /etc/systemd/system/
sudo udevadm control --reload-rules && udevadm trigger
sudo apt install -y can-utils

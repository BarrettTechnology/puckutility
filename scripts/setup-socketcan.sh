sudo cp reset_can.sh /usr/bin
sudo cp 60-can.rules /etc/udev/rules.d
sudo udevadm control --reload-rules
sudo udevadm trigger
#sh install_pcan.sh # removing pcan usage

if grep can0 /etc/network/interfaces -q; then
  echo "can0 already installed"
  exit 1
fi
echo "Installing can0..."
echo "auto can0
iface can0 inet manual
    pre-up /sbin/ip link set can0 type can bitrate 1000000 txqueuelen 1000
    ip link set can0 txqueuelen 1000
    up /sbin/ip link set can0 up
    down /sbin/ip link set can0 down" | sudo tee -a /etc/network/interfaces > /dev/null
sudo apt install -y can-utils
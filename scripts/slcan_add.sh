#!/bin/sh
# Bind the USBCAN device
echo "config slcan0"
slcand  -f -s8 -o -t hw -S 3000000 /dev/$1 can0
sleep 1
ip link set can0 txqueuelen 1000
ip link set can0 restart-ms 100
ip link set can0 bitrate 1000000
ip link set can0 up

#if [ -z ${DEVNAME+x} ]; then
#        if [ -z ${1+x} ]; then
#                echo "No device $DEVNAME"
#                exit 1
#        fi
#        DEVNAME=${1}
#fi
# Read device name and extract the tty/usb# (id)
#ID=$(echo ${DEVNAME} | grep -o -E '[0-9]+')
# create new slcan network device
#slcand -f -s8 -o  $DEVNAME can${ID} &> /dev/null
#sleep 1 
#ip link set up can${ID}

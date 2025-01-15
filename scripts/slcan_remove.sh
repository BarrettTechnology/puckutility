#!/bin/sh
# Remove the USBCAN device
#pkill slcand
ID=$(echo ${DEVNAME} | grep -o -E '[0-9]+')
PROCESS=$(echo ps ax | pgrep -f slcan[$ID])
kill $PROCESS
echo ${DEVNAME}
echo "slcand killed!!!!!!"


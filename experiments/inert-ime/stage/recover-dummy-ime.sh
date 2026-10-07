#!/system/bin/sh
# Admission failure recovery only: no display or framework service changes.
HELPER=$1
while :; do
    sh "$HELPER" restore final && exit 0
    sleep 5
done

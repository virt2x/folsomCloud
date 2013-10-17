#!/bin/bash

while (true)
do
    time=`date | awk '{print $4}'`
    for n in 2 3 4 5
    do
        ssh server$n "date -s $time"
        echo $time
    done
    sleep 30
    echo "ok"
done

#!/bin/bash

# delete all log one day
while (true)
do
    for x in `ls /var/log/apache2`
    do
        echo  > /var/log/apache2/$x
    done
    for n in 1 2 3 4 5
    do
        for x in `ssh server$n "ls /var/log/nova/"`
        do
            ssh server$n "echo >/var/log/nova/$x"
        done
    done
    sleep 86400
done


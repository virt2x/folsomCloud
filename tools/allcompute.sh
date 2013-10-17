#!/bin/bash
for n in 1 2 3 4 5
do
ssh server$n "/etc/init.d/libvirt-bin restart"
ssh server$n "/root/res nova-compute"
done

for n in 1 2 3 4 5
do
    echo "---------------Server $n ------------------"
    out=`ssh server$n "ps aux | grep compute " | grep compute | grep python | wc -l`
    while [ $out -eq 0 ]
    do
        ssh server$n "ps aux | grep compute | grep python"
        out=`ssh server$n "ps aux | grep compute " | grep compute | grep python | wc -l`
    done
done

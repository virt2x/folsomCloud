#!/bin/bash


for n in 1 2 3 4 5
do
    server=192.168.111.$n
    ssh $server "sed -i \"s,127.0.1.1.*,127.0.1.1        server$n,g\" /etc/hosts"
    ssh $server "sed -i \"/192.168/d\" /etc/hosts"
    ssh $server "for n in 1 2 3 4 5; do echo \"192.168.111.$n server$n\" >> /etc/hosts"
    scp /root/os/tools/ssh.sh $server:/root/os/tools/
    ssh $server "apt-get install expect"
    ssh $server "mkdir -p /root/os/tools"
    ssh $server "rm -rfv /root/.ssh"
    scp /root/os/tools/ssh.sh server$n:/root/os/tools/
    ssh $server "/root/os/tools/ssh.sh"
    scp $server:/root/.ssh/id_rsa.pub /tmp/server$n
done

cat /tmp/server1 /tmp/server2 /tmp/server3 /tmp/server4 /tmp/server5 >/root/.ssh/authorized_keys

for n in 2 3 4 5
do
    server=192.168.111.$n
    scp /root/.ssh/authorized_keys $server:/root/.ssh/
done

for n in 1 2 3 4 5
do
    server=192.168.111.$n
    ssh $server "sed -i \"/192.168/d\" /etc/hosts"
    for x in 1 2 3 4 5
    do
        if [[ ! $n -eq $x ]]; then
            ssh $server "echo 192.168.111.$x server$x >> /etc/hosts"
        fi
    done
done


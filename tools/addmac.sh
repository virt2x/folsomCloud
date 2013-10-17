#!/bin/bash


file=/etc/udev/rules.d/70-persistent-net.rules

function _vimrc() {
cat << "EOF" > /root/.vimrc
set shiftwidth=4
set tabstop=4
set expandtab
set noswapfile
EOF
}

function _template() {
cat <<"EOF" > $file
SUBSYSTEM=="net", ACTION=="add", DRIVERS=="?*", ATTR{address}=="%ETH0%", ATTR{dev_id}=="0x0", ATTR{type}=="1", KERNEL=="eth*", NAME="eth0"
SUBSYSTEM=="net", ACTION=="add", DRIVERS=="?*", ATTR{address}=="%ETH1%", ATTR{dev_id}=="0x0", ATTR{type}=="1", KERNEL=="eth*", NAME="eth1"
EOF
}

function get_mac() {
    newstr=`tr '[a-z]' '[A-Z]' <<<"$1"`
    mac=`ifconfig $1 | grep HWaddr | awk '{print $5}'`
    sed -i "s,%${newstr}%,${mac},g" $file
}

function main() {
    _vimrc
    _template
    local cnt=`lspci | grep Eth | wc -l`
    n=0
    while [[ ! $n -eq $cnt ]]; do
        echo $n
        get_mac eth$n
        let n=n+1
    done
}

main

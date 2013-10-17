#!/bin/bash

# Configure libvirt
sed -i 's/libvirtd_opts=.*$/libvirtd_opts=\"-d -l\"/' /etc/default/libvirt-bin
sed -i 's/#listen_tls/listen_tls/ ; s/#listen_tcp/listen_tcp/ ; s/#auth_tcp.*$/auth_tcp = \"none\"/ ' /etc/libvirt/libvirtd.conf
uuid=`uuidgen`
echo host_uuid = \"$uuid\" >> /etc/libvirt/libvirtd.conf
sed -i '{
                s/#max_clients = 20/max_clients = 200/;
                s/#max_workers = 20/max_workers = 200/;
                s/#max_requests = 20/max_requests = 200/;
                s/#max_client_requests = 5/max_client_requests = 100/;
}' /etc/libvirt/libvirtd.conf

#!/usr/bin/expect -f
spawn ssh-keygen
expect {
"Enter file in which to save the key (/root/.ssh/id_rsa):" {send "\r"; exp_continue }
"Enter passphrase (empty for no passphrase):" { send "\r"; exp_continue }
"Enter same passphrase again:" { send "\r" }
}
expect eof

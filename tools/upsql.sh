#!/bin/bash

/etc/init.d/mysql stop
mysqld_safe --user=mysql --skip-grant-tables --skip-networking &
mysql -uroot -pnova -e "use mysql; UPDATE user SET Password=PASSWORD('nova') where USER='root'; FLUSH PRIVILEGES;"
/etc/init.d/mysql restart

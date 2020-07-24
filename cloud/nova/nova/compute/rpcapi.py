# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2012, Red Hat, Inc.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Client side of the compute RPC API.
"""

from nova import config
from nova import exception
from nova import flags
from nova.openstack.common import jsonutils
from nova.openstack.common import rpc
import nova.openstack.common.rpc.proxy

CONF = config.CONF


def _compute_topic(topic, ctxt, host, instance):
    '''Get the topic to use for a message.

    :param topic: the base topic
    :param ctxt: request context
    :param host: explicit host to send the message to.
    :param instance: If an explicit host was not specified, use
                     instance['host']

    :returns: A topic string
    '''
    if not host:
        if not instance:
            raise exception.NovaException(_('No compute host specified'))
        host = instance['host']
    if not host:
        raise exception.NovaException(_('Unable to find host for '
                                           'Instance %s') % instance['uuid'])
    return rpc.queue_get_for(ctxt, topic, host)


class ComputeAPI(nova.openstack.common.rpc.proxy.RpcProxy):
    '''Client side of the compute rpc API.

    API version history:

        1.0 - Initial version.
        1.1 - Adds get_host_uptime()
        1.2 - Adds check_can_live_migrate_[destination|source]
        1.3 - Adds change_instance_metadata()
        1.4 - Remove instance_uuid, add instance argument to reboot_instance()
        1.5 - Remove instance_uuid, add instance argument to pause_instance(),
              unpause_instance()
        1.6 - Remove instance_uuid, add instance argument to suspend_instance()
        1.7 - Remove instance_uuid, add instance argument to
              get_console_output()
        1.8 - Remove instance_uuid, add instance argument to
              add_fixed_ip_to_instance()
        1.9 - Remove instance_uuid, add instance argument to attach_volume()
        1.10 - Remove instance_id, add instance argument to
               check_can_live_migrate_destination()
        1.11 - Remove instance_id, add instance argument to
               check_can_live_migrate_source()
        1.12 - Remove instance_uuid, add instance argument to confirm_resize()
        1.13 - Remove instance_uuid, add instance argument to detach_volume()
        1.14 - Remove instance_uuid, add instance argument to finish_resize()
        1.15 - Remove instance_uuid, add instance argument to
               finish_revert_resize()
        1.16 - Remove instance_uuid, add instance argument to get_diagnostics()
        1.17 - Remove instance_uuid, add instance argument to get_vnc_console()
        1.18 - Remove instance_uuid, add instance argument to inject_file()
        1.19 - Remove instance_uuid, add instance argument to
               inject_network_info()
        1.20 - Remove instance_id, add instance argument to
               post_live_migration_at_destination()
        1.21 - Remove instance_uuid, add instance argument to
               power_off_instance() and stop_instance()
        1.22 - Remove instance_uuid, add instance argument to
               power_on_instance() and start_instance()
        1.23 - Remove instance_id, add instance argument to
               pre_live_migration()
        1.24 - Remove instance_uuid, add instance argument to
               rebuild_instance()
        1.25 - Remove instance_uuid, add instance argument to
               remove_fixed_ip_from_instance()
        1.26 - Remove instance_id, add instance argument to
               remove_volume_connection()
        1.27 - Remove instance_uuid, add instance argument to
               rescue_instance()
        1.28 - Remove instance_uuid, add instance argument to reset_network()
        1.29 - Remove instance_uuid, add instance argument to resize_instance()
        1.30 - Remove instance_uuid, add instance argument to resume_instance()
        1.31 - Remove instance_uuid, add instance argument to revert_resize()
        1.32 - Remove instance_id, add instance argument to
               rollback_live_migration_at_destination()
        1.33 - Remove instance_uuid, add instance argument to
               set_admin_password()
        1.34 - Remove instance_uuid, add instance argument to
               snapshot_instance()
        1.35 - Remove instance_uuid, add instance argument to
               unrescue_instance()
        1.36 - Remove instance_uuid, add instance argument to
               change_instance_metadata()
        1.37 - Remove instance_uuid, add instance argument to
               terminate_instance()
        1.38 - Changes to prep_resize():
                - remove instance_uuid, add instance
                - remove instance_type_id, add instance_type
                - remove topic, it was unused
        1.39 - Remove instance_uuid, add instance argument to run_instance()
        1.40 - Remove instance_id, add instance argument to live_migration()
        1.41 - Adds refresh_instance_security_rules()
        1.42 - Add reservations arg to prep_resize(), resize_instance(),
               finish_resize(), confirm_resize(), revert_resize() and
               finish_revert_resize()
        1.43 - Add migrate_data to live_migration()
        1.44 - Adds reserve_block_device_name()

        2.0 - Remove 1.x backwards compat
        2.1 - Adds orig_sys_metadata to rebuild_instance()
        2.2 - Adds subordinate_info parameter to add_aggregate_host() and
              remove_aggregate_host()
        2.3 - Adds volume_id to reserve_block_device_name()
        2.4 - Add bdms to terminate_instance
        2.5 - Add block device and network info to reboot_instance
        2.6 - Remove migration_id, add migration to resize_instance
        2.7 - Remove migration_id, add migration to confirm_resize
        2.8 - Remove migration_id, add migration to finish_resize
        2.9 - Add publish_service_capabilities()
        2.10 - Adds filter_properties and request_spec to prep_resize()
        2.11 - Adds soft_delete_instance() and restore_instance()
        2.12 - Remove migration_id, add migration to revert_resize
        2.13 - Remove migration_id, add migration to finish_revert_resize
        2.14 - Remove aggregate_id, add aggregate to add_aggregate_host
        2.15 - Remove aggregate_id, add aggregate to remove_aggregate_host
        2.16 - Add instance_type to resize_instance
    '''

    #
    # NOTE(russellb): This is the default minimum version that the server
    # (manager) side must implement unless otherwise specified using a version
    # argument to self.call()/cast()/etc. here.  It should be left as X.0 where
    # X is the current major API version (1.0, 2.0, ...).  For more information
    # about rpc API versioning, see the docs in
    # openstack/common/rpc/dispatcher.py.
    #
    BASE_RPC_API_VERSION = '2.0'

    def __init__(self):
        super(ComputeAPI, self).__init__(
                topic=CONF.compute_topic,
                default_version=self.BASE_RPC_API_VERSION)

    def add_aggregate_host(self, ctxt, aggregate, host_param, host,
                           subordinate_info=None):
        '''Add aggregate host.

        :param ctxt: request context
        :param aggregate_id:
        :param host_param: This value is placed in the message to be the 'host'
                           parameter for the remote method.
        :param host: This is the host to send the message to.
        '''

        aggregate_p = jsonutils.to_primitive(aggregate)
        self.cast(ctxt, self.make_msg('add_aggregate_host',
                aggregate=aggregate_p, host=host_param,
                subordinate_info=subordinate_info),
                topic=_compute_topic(self.topic, ctxt, host, None),
                version='2.14')

    def add_fixed_ip_to_instance(self, ctxt, instance, network_id):
        instance_p = jsonutils.to_primitive(instance)
        self.cast(ctxt, self.make_msg('add_fixed_ip_to_instance',
                instance=instance_p, network_id=network_id),
                topic=_compute_topic(self.topic, ctxt, None, instance))

    def attach_volume(self, ctxt, instance, volume_id, mountpoint):
        instance_p = jsonutils.to_primitive(instance)
        self.cast(ctxt, self.make_msg('attach_volume',
                instance=instance_p, volume_id=volume_id,
                mountpoint=mountpoint),
                topic=_compute_topic(self.topic, ctxt, None, instance))

    def change_instance_metadata(self, ctxt, instance, diff):
        instance_p = jsonutils.to_primitive(instance)
        self.cast(ctxt, self.make_msg('change_instance_metadata',
                  instance=instance_p, diff=diff),
                  topic=_compute_topic(self.topic, ctxt, None, instance))

    def check_can_live_migrate_destination(self, ctxt, instance, destination,
                                           block_migration, disk_over_commit):
        instance_p = jsonutils.to_primitive(instance)
        return self.call(ctxt,
                         self.make_msg('check_can_live_migrate_destination',
                                       instance=instance_p,
                                       block_migration=block_migration,
                                       disk_over_commit=disk_over_commit),
                         topic=_compute_topic(self.topic,
                                              ctxt, destination, None))

    def check_can_live_migrate_source(self, ctxt, instance, dest_check_data):
        instance_p = jsonutils.to_primitive(instance)
        self.call(ctxt, self.make_msg('check_can_live_migrate_source',
                           instance=instance_p,
                           dest_check_data=dest_check_data),
                  topic=_compute_topic(self.topic, ctxt, None, instance))

    def confirm_resize(self, ctxt, instance, migration, host,
            reservations=None, cast=True):
        rpc_method = self.cast if cast else self.call
        instance_p = jsonutils.to_primitive(instance)
        migration_p = jsonutils.to_primitive(migration)
        return rpc_method(ctxt, self.make_msg('confirm_resize',
                instance=instance_p, migration=migration_p,
                reservations=reservations),
                topic=_compute_topic(self.topic, ctxt, host, instance),
                version='2.7')

    def detach_volume(self, ctxt, instance, volume_id):
        instance_p = jsonutils.to_primitive(instance)
        self.cast(ctxt, self.make_msg('detach_volume',
                instance=instance_p, volume_id=volume_id),
                topic=_compute_topic(self.topic, ctxt, None, instance))

    def finish_resize(self, ctxt, instance, migration, image, disk_info,
            host, reservations=None):
        instance_p = jsonutils.to_primitive(instance)
        migration_p = jsonutils.to_primitive(migration)
        self.cast(ctxt, self.make_msg('finish_resize',
                instance=instance_p, migration=migration_p,
                image=image, disk_info=disk_info, reservations=reservations),
                topic=_compute_topic(self.topic, ctxt, host, None),
                version='2.8')

    def finish_revert_resize(self, ctxt, instance, migration, host,
                             reservations=None):
        instance_p = jsonutils.to_primitive(instance)
        migration_p = jsonutils.to_primitive(migration)
        self.cast(ctxt, self.make_msg('finish_revert_resize',
                instance=instance_p, migration=migration_p,
                reservations=reservations),
                topic=_compute_topic(self.topic, ctxt, host, None),
                version='2.13')

    def get_console_output(self, ctxt, instance, tail_length):
        instance_p = jsonutils.to_primitive(instance)
        return self.call(ctxt, self.make_msg('get_console_output',
                instance=instance_p, tail_length=tail_length),
                topic=_compute_topic(self.topic, ctxt, None, instance))

    def get_console_pool_info(self, ctxt, console_type, host):
        return self.call(ctxt, self.make_msg('get_console_pool_info',
                console_type=console_type),
                topic=_compute_topic(self.topic, ctxt, host, None))

    def get_console_topic(self, ctxt, host):
        return self.call(ctxt, self.make_msg('get_console_topic'),
                topic=_compute_topic(self.topic, ctxt, host, None))

    def get_diagnostics(self, ctxt, instance):
        instance_p = jsonutils.to_primitive(instance)
        return self.call(ctxt, self.make_msg('get_diagnostics',
                instance=instance_p),
                topic=_compute_topic(self.topic, ctxt, None, instance))

    def get_vnc_console(self, ctxt, instance, console_type):
        instance_p = jsonutils.to_primitive(instance)
        return self.call(ctxt, self.make_msg('get_vnc_console',
                instance=instance_p, console_type=console_type),
                topic=_compute_topic(self.topic, ctxt, None, instance))

    def host_maintenance_mode(self, ctxt, host_param, mode, host):
        '''Set host maintenance mode

        :param ctxt: request context
        :param host_param: This value is placed in the message to be the 'host'
                           parameter for the remote method.
        :param mode:
        :param host: This is the host to send the message to.
        '''
        return self.call(ctxt, self.make_msg('host_maintenance_mode',
                host=host_param, mode=mode),
                topic=_compute_topic(self.topic, ctxt, host, None))

    def host_power_action(self, ctxt, action, host):
        topic = _compute_topic(self.topic, ctxt, host, None)
        return self.call(ctxt, self.make_msg('host_power_action',
                action=action), topic)

    def inject_file(self, ctxt, instance, path, file_contents):
        instance_p = jsonutils.to_primitive(instance)
        self.cast(ctxt, self.make_msg('inject_file',
                instance=instance_p, path=path,
                file_contents=file_contents),
                topic=_compute_topic(self.topic, ctxt, None, instance))

    def inject_network_info(self, ctxt, instance):
        instance_p = jsonutils.to_primitive(instance)
        self.cast(ctxt, self.make_msg('inject_network_info',
                instance=instance_p),
                topic=_compute_topic(self.topic, ctxt, None, instance))

    def live_migration(self, ctxt, instance, dest, block_migration, host,
                       migrate_data=None):
        instance_p = jsonutils.to_primitive(instance)
        self.cast(ctxt, self.make_msg('live_migration', instance=instance_p,
                dest=dest, block_migration=block_migration,
                migrate_data=migrate_data),
                topic=_compute_topic(self.topic, ctxt, host, None))

    def pause_instance(self, ctxt, instance):
        instance_p = jsonutils.to_primitive(instance)
        self.cast(ctxt, self.make_msg('pause_instance',
                instance=instance_p),
                topic=_compute_topic(self.topic, ctxt, None, instance))

    def post_live_migration_at_destination(self, ctxt, instance,
            block_migration, host):
        instance_p = jsonutils.to_primitive(instance)
        return self.call(ctxt,
                self.make_msg('post_live_migration_at_destination',
                instance=instance_p, block_migration=block_migration),
                _compute_topic(self.topic, ctxt, host, None))

    def power_off_instance(self, ctxt, instance):
        instance_p = jsonutils.to_primitive(instance)
        self.cast(ctxt, self.make_msg('power_off_instance',
                instance=instance_p),
                topic=_compute_topic(self.topic, ctxt, None, instance))

    def power_on_instance(self, ctxt, instance):
        instance_p = jsonutils.to_primitive(instance)
        self.cast(ctxt, self.make_msg('power_on_instance',
                instance=instance_p),
                topic=_compute_topic(self.topic, ctxt, None, instance))

    def pre_live_migration(self, ctxt, instance, block_migration, disk,
            host):
        instance_p = jsonutils.to_primitive(instance)
        return self.call(ctxt, self.make_msg('pre_live_migration',
                instance=instance_p, block_migration=block_migration,
                disk=disk), _compute_topic(self.topic, ctxt, host, None))

    def prep_resize(self, ctxt, image, instance, instance_type, host,
                    reservations=None, request_spec=None,
                    filter_properties=None):
        instance_p = jsonutils.to_primitive(instance)
        instance_type_p = jsonutils.to_primitive(instance_type)
        self.cast(ctxt, self.make_msg('prep_resize',
                instance=instance_p, instance_type=instance_type_p,
                image=image, reservations=reservations,
                request_spec=request_spec,
                filter_properties=filter_properties),
                _compute_topic(self.topic, ctxt, host, None),
                version='2.10')

    def reboot_instance(self, ctxt, instance,
                        block_device_info, network_info, reboot_type):
        instance_p = jsonutils.to_primitive(instance)
        self.cast(ctxt, self.make_msg('reboot_instance',
                instance=instance_p,
                block_device_info=block_device_info,
                network_info=network_info,
                reboot_type=reboot_type),
                topic=_compute_topic(self.topic, ctxt, None, instance),
                version='2.5')

    def rebuild_instance(self, ctxt, instance, new_pass, injected_files,
            image_ref, orig_image_ref, orig_sys_metadata):
        instance_p = jsonutils.to_primitive(instance)
        self.cast(ctxt, self.make_msg('rebuild_instance',
                instance=instance_p, new_pass=new_pass,
                injected_files=injected_files, image_ref=image_ref,
                orig_image_ref=orig_image_ref,
                orig_sys_metadata=orig_sys_metadata),
                topic=_compute_topic(self.topic, ctxt, None, instance),
                version='2.1')

    def refresh_provider_fw_rules(self, ctxt, host):
        self.cast(ctxt, self.make_msg('refresh_provider_fw_rules'),
                _compute_topic(self.topic, ctxt, host, None))

    def remove_aggregate_host(self, ctxt, aggregate, host_param, host,
                              subordinate_info=None):
        '''Remove aggregate host.

        :param ctxt: request context
        :param aggregate_id:
        :param host_param: This value is placed in the message to be the 'host'
                           parameter for the remote method.
        :param host: This is the host to send the message to.
        '''

        aggregate_p = jsonutils.to_primitive(aggregate)
        self.cast(ctxt, self.make_msg('remove_aggregate_host',
                aggregate=aggregate_p, host=host_param,
                subordinate_info=subordinate_info),
                topic=_compute_topic(self.topic, ctxt, host, None),
                version='2.15')

    def remove_fixed_ip_from_instance(self, ctxt, instance, address):
        instance_p = jsonutils.to_primitive(instance)
        self.cast(ctxt, self.make_msg('remove_fixed_ip_from_instance',
                instance=instance_p, address=address),
                topic=_compute_topic(self.topic, ctxt, None, instance))

    def remove_volume_connection(self, ctxt, instance, volume_id, host):
        instance_p = jsonutils.to_primitive(instance)
        return self.call(ctxt, self.make_msg('remove_volume_connection',
                instance=instance_p, volume_id=volume_id),
                topic=_compute_topic(self.topic, ctxt, host, None))

    def rescue_instance(self, ctxt, instance, rescue_password):
        instance_p = jsonutils.to_primitive(instance)
        self.cast(ctxt, self.make_msg('rescue_instance',
                instance=instance_p,
                rescue_password=rescue_password),
                topic=_compute_topic(self.topic, ctxt, None, instance))

    def reset_network(self, ctxt, instance):
        instance_p = jsonutils.to_primitive(instance)
        self.cast(ctxt, self.make_msg('reset_network',
                instance=instance_p),
                topic=_compute_topic(self.topic, ctxt, None, instance))

    def resize_instance(self, ctxt, instance, migration, image, instance_type,
                        reservations=None):
        topic = _compute_topic(self.topic, ctxt, None, instance)
        instance_p = jsonutils.to_primitive(instance)
        migration_p = jsonutils.to_primitive(migration)
        instance_type_p = jsonutils.to_primitive(instance_type)
        self.cast(ctxt, self.make_msg('resize_instance',
                instance=instance_p, migration=migration_p,
                image=image, reservations=reservations,
                instance_type=instance_type_p), topic,
                version='2.16')

    def resume_instance(self, ctxt, instance):
        instance_p = jsonutils.to_primitive(instance)
        self.cast(ctxt, self.make_msg('resume_instance',
                instance=instance_p),
                topic=_compute_topic(self.topic, ctxt, None, instance))

    def revert_resize(self, ctxt, instance, migration, host,
                      reservations=None):
        instance_p = jsonutils.to_primitive(instance)
        migration_p = jsonutils.to_primitive(migration)
        self.cast(ctxt, self.make_msg('revert_resize',
                instance=instance_p, migration=migration_p,
                reservations=reservations),
                topic=_compute_topic(self.topic, ctxt, host, instance),
                version='2.12')

    def rollback_live_migration_at_destination(self, ctxt, instance, host):
        instance_p = jsonutils.to_primitive(instance)
        self.cast(ctxt, self.make_msg('rollback_live_migration_at_destination',
            instance=instance_p),
            topic=_compute_topic(self.topic, ctxt, host, None))

    def run_instance(self, ctxt, instance, host, request_spec,
                     filter_properties, requested_networks,
                     injected_files, admin_password,
                     is_first_time):
        instance_p = jsonutils.to_primitive(instance)
        self.cast(ctxt, self.make_msg('run_instance', instance=instance_p,
                request_spec=request_spec, filter_properties=filter_properties,
                requested_networks=requested_networks,
                injected_files=injected_files, admin_password=admin_password,
                is_first_time=is_first_time),
                topic=_compute_topic(self.topic, ctxt, host, None))

    def set_admin_password(self, ctxt, instance, new_pass):
        instance_p = jsonutils.to_primitive(instance)
        return self.call(ctxt, self.make_msg('set_admin_password',
                instance=instance_p, new_pass=new_pass),
                topic=_compute_topic(self.topic, ctxt, None, instance))

    def set_host_enabled(self, ctxt, enabled, host):
        topic = _compute_topic(self.topic, ctxt, host, None)
        return self.call(ctxt, self.make_msg('set_host_enabled',
                enabled=enabled), topic)

    def get_host_uptime(self, ctxt, host):
        topic = _compute_topic(self.topic, ctxt, host, None)
        return self.call(ctxt, self.make_msg('get_host_uptime'), topic)

    def reserve_block_device_name(self, ctxt, instance, device, volume_id):
        instance_p = jsonutils.to_primitive(instance)
        return self.call(ctxt, self.make_msg('reserve_block_device_name',
                instance=instance_p, device=device, volume_id=volume_id),
                topic=_compute_topic(self.topic, ctxt, None, instance),
                version='2.3')

    def snapshot_instance(self, ctxt, instance, image_id, image_type,
            backup_type, rotation):
        instance_p = jsonutils.to_primitive(instance)
        self.cast(ctxt, self.make_msg('snapshot_instance',
                instance=instance_p, image_id=image_id,
                image_type=image_type, backup_type=backup_type,
                rotation=rotation),
                topic=_compute_topic(self.topic, ctxt, None, instance))

    def start_instance(self, ctxt, instance):
        instance_p = jsonutils.to_primitive(instance)
        self.cast(ctxt, self.make_msg('start_instance',
                instance=instance_p),
                topic=_compute_topic(self.topic, ctxt, None, instance))

    def stop_instance(self, ctxt, instance, cast=True):
        rpc_method = self.cast if cast else self.call
        instance_p = jsonutils.to_primitive(instance)
        return rpc_method(ctxt, self.make_msg('stop_instance',
                instance=instance_p),
                topic=_compute_topic(self.topic, ctxt, None, instance))

    def suspend_instance(self, ctxt, instance):
        instance_p = jsonutils.to_primitive(instance)
        self.cast(ctxt, self.make_msg('suspend_instance',
                instance=instance_p),
                topic=_compute_topic(self.topic, ctxt, None, instance))

    def terminate_instance(self, ctxt, instance, bdms):
        instance_p = jsonutils.to_primitive(instance)
        bdms_p = jsonutils.to_primitive(bdms)
        self.cast(ctxt, self.make_msg('terminate_instance',
                instance=instance_p, bdms=bdms_p),
                topic=_compute_topic(self.topic, ctxt, None, instance),
                version='2.4')

    def unpause_instance(self, ctxt, instance):
        instance_p = jsonutils.to_primitive(instance)
        self.cast(ctxt, self.make_msg('unpause_instance',
                instance=instance_p),
                topic=_compute_topic(self.topic, ctxt, None, instance))

    def unrescue_instance(self, ctxt, instance):
        instance_p = jsonutils.to_primitive(instance)
        self.cast(ctxt, self.make_msg('unrescue_instance',
                instance=instance_p),
                topic=_compute_topic(self.topic, ctxt, None, instance))

    def publish_service_capabilities(self, ctxt):
        self.fanout_cast(ctxt, self.make_msg('publish_service_capabilities'))

    def soft_delete_instance(self, ctxt, instance):
        instance_p = jsonutils.to_primitive(instance)
        self.cast(ctxt, self.make_msg('soft_delete_instance',
                instance=instance_p),
                topic=_compute_topic(self.topic, ctxt, None, instance))

    def restore_instance(self, ctxt, instance):
        instance_p = jsonutils.to_primitive(instance)
        self.cast(ctxt, self.make_msg('restore_instance',
                instance=instance_p),
                topic=_compute_topic(self.topic, ctxt, None, instance))


class SecurityGroupAPI(nova.openstack.common.rpc.proxy.RpcProxy):
    '''Client side of the security group rpc API.

    API version history:

        1.0 - Initial version.
        1.41 - Adds refresh_instance_security_rules()

        2.0 - Remove 1.x backwards compat
    '''

    #
    # NOTE(russellb): This is the default minimum version that the server
    # (manager) side must implement unless otherwise specified using a version
    # argument to self.call()/cast()/etc. here.  It should be left as X.0 where
    # X is the current major API version (1.0, 2.0, ...).  For more information
    # about rpc API versioning, see the docs in
    # openstack/common/rpc/dispatcher.py.
    #
    BASE_RPC_API_VERSION = '2.0'

    def __init__(self):
        super(SecurityGroupAPI, self).__init__(
                topic=CONF.compute_topic,
                default_version=self.BASE_RPC_API_VERSION)

    def refresh_security_group_rules(self, ctxt, security_group_id, host):
        self.cast(ctxt, self.make_msg('refresh_security_group_rules',
                security_group_id=security_group_id),
                topic=_compute_topic(self.topic, ctxt, host, None))

    def refresh_security_group_members(self, ctxt, security_group_id,
            host):
        self.cast(ctxt, self.make_msg('refresh_security_group_members',
                security_group_id=security_group_id),
                topic=_compute_topic(self.topic, ctxt, host, None))

    def refresh_instance_security_rules(self, ctxt, host, instance):
        instance_p = jsonutils.to_primitive(instance)
        self.cast(ctxt, self.make_msg('refresh_instance_security_rules',
                instance=instance_p),
                topic=_compute_topic(self.topic, ctxt, instance['host'],
                instance))

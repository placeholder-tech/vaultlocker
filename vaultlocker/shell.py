# -*- coding: utf-8 -*-

# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import argparse
import hvac
import logging
import os
import socket
import tenacity
import uuid

from six.moves import configparser
import subprocess
from vaultlocker import dmcrypt
from vaultlocker import exceptions
from vaultlocker import systemd

logger = logging.getLogger(__name__)

RUN_VAULTLOCKER = '/run/vaultlocker'
DEFAULT_CONF_FILE = '/etc/vaultlocker/vaultlocker.conf'


def _vault_client(config):
    """Helper wrapper to create Vault Client

    :param: config: configparser object of vaultlocker config
    :returns: hvac.Client. configured Vault Client object
    """
    client = hvac.Client(
        url=config.get('vault', 'url'),
        token=config.get('vault', 'token', fallback=None),
        verify=config.get('vault', 'ca_bundle', fallback=True),
        namespace=config.get('vault', 'namespace', fallback=None)
    )
    if not config.get('vault', 'role_id', fallback=None) is None:
        role_id = config.get('vault', 'role_id')
    if not config.get('vault', 'approle', fallback=None) is None:
        role_id = config.get('vault', 'approle')
    client.auth.approle.login(
        role_id,
        secret_id=config.get('vault', 'secret_id', fallback=None)
    )
    return client


def _get_vault_path(device_uuid):
    """Generate full vault path for a given block device UUID

    :param: device_uuid: String of the device UUID
    :returns: str: Path to vault resource for device
    """
    # return '{}/{}'.format(
    #     socket.gethostname(),
    #     device_uuid
    # )

    return device_uuid

def _encrypt_block_device(args, client, config):
    """Encrypt and open a block device

    Stores the dm-crypt key direct in vault

    :param: args: argparser generated cli arguments
    :param: client: hvac.Client for Vault access
    :param: config: configparser object of vaultlocker config
    """
    block_device = args.block_device[0]
    key = dmcrypt.generate_key()
    block_uuid = str(uuid.uuid4()) if not args.uuid else args.uuid
    vault_path = _get_vault_path(block_uuid)
    service_name = 'vaultlocker-decrypt@{}.service'.format(block_uuid)
    mount_point = config.get('vault', 'backend')

    # if systemd.service_enabled(service_name):
    #     logger.info('Systemd unit file exists for {} '
    #                 'exiting...'.format(block_uuid))
    #     raise exceptions.VaultlockerException(
    #         'Block UUID {} was already encrypted.'.format(block_uuid)
    #     )

    # NOTE: store and validate key before trying to encrypt disk
    try:
        if config.get('vault', 'kv_version') == '2':
            client.secrets.kv.v2.create_or_update_secret(
                vault_path,
                secret=dict(dmcrypt_key=key),
                mount_point=mount_point
            )
        elif config.get('vault', 'kv_version') == '1':
            client.secrets.kv.v1.create_or_update_secret(
                vault_path,
                secret=dict(dmcrypt_key=key),
                mount_point=mount_point
            )
    except hvac.exceptions.VaultError as write_error:
        logger.error(
            'Vault write to path {}. Failed with error: {}'.format(
                vault_path, write_error))
        raise exceptions.VaultWriteError(vault_path, write_error)

    try:
        if config.get('vault', 'kv_version') == '2':
            stored_data = client.secrets.kv.v2.read_secret_version(
                vault_path,
                mount_point=mount_point
            )['data']['data']
        elif config.get('vault', 'kv_version') == '1':
            stored_data = client.secrets.kv.v1.read_secret(
                vault_path,
                mount_point=mount_point
            )['data']
    except hvac.exceptions.VaultError as read_error:
        logger.error('Vault access to path {}/{}'
                     'failed with error: {}'
                     .format(
                         mount_point,
                         vault_path,
                         read_error
                     )
                     )
        raise exceptions.VaultReadError(vault_path, read_error)

    if not key == stored_data['dmcrypt_key']:
        raise exceptions.VaultKeyMismatch(vault_path)

    # All function calls within try/catch raise a CalledProcessError
    # if return code is non-zero
    # This way if any of the calls fail, the key can be removed from vault
    try:
        dmcrypt.luks_format(key, block_device, block_uuid)
        # Ensure sym link for new encrypted device is created
        # LP Bug #1780332
        dmcrypt.udevadm_rescan(block_device)
        dmcrypt.udevadm_settle(block_uuid)
        dmcrypt.luks_open(key, block_uuid)
    except subprocess.CalledProcessError as luks_error:
        logger.error(
            'LUKS formatting {} failed with error code: {}\n'
            'LUKS output: {}'.format(
                block_device,
                luks_error.returncode,
                luks_error.output))

        try:
            if config.get('vault', 'kv_version') == '2':
                client.secrets.kv.v2.delete_metadata_and_all_versions(
                    vault_path,
                    mount_point=mount_point
                )
            elif config.get('vault', 'kv_version') == '1':
                client.secrets.kv.v1.delete_secret(
                    vault_path,
                    mount_point=mount_point
                )
        except hvac.exceptions.VaultError as del_error:
            raise exceptions.VaultDeleteError(vault_path, del_error)

        raise exceptions.LUKSFailure(block_device, luks_error.output)

    #systemd.enable(service_name)


def _decrypt_block_device(args, client, config):
    """Open a LUKS/dm-crypt encrypted block device

    The devices dm-crypt key is retrieved from Vault

    :param: args: argparser generated cli arguments
    :param: client: hvac.Client for Vault access
    :param: config: configparser object of vaultlocker config
    """
    block_uuid = args.uuid[0]
    if _device_exists(block_uuid):
        logger.info('Skipping setup of {} because '
                    'it already exists.'.format(block_uuid))
        return
    stored_data = None
    vault_path = _get_vault_path(block_uuid)
    mount_point = config.get('vault', 'backend')

    try:
        if config.get('vault', 'kv_version') == '2':
            stored_data = client.secrets.kv.v2.read_secret_version(
                vault_path,
                mount_point=mount_point
            )['data']['data']
        elif config.get('vault', 'kv_version') == '1':
            stored_data = client.secrets.kv.v1.read_secret(
                vault_path,
                mount_point=mount_point
            )['data']
    except Exception:
        logger.info('Couldn\'t read the secret at key: {}/{}'
                    .format(mount_point, vault_path))

    if stored_data is None:
        raise ValueError('Unable to locate key for {}'.format(block_uuid))
    key = stored_data['dmcrypt_key']

    dmcrypt.luks_open(key, block_uuid)


def _device_exists(block_uuid):
    """Checks if the device already exists."""
    handle = 'crypt-{}'.format(block_uuid)
    path = "/dev/mapper/{}".format(handle)
    logger.info('Checking if {} exists.'.format(path))
    return os.path.exists(path)


def _do_it_with_persistence(func, args, config):
    """Exec func with retries based on provided cli flags

    :param: func: function to attempt to execute
    :param: args: argparser generated cli arguments
    :param: config: configparser object of vaultlocker config
    """
    @tenacity.retry(
        wait=tenacity.wait_fixed(1),
        reraise=True,
        stop=(
            tenacity.stop_after_delay(args.retry) if args.retry > 0
            else tenacity.stop_after_attempt(1)
            ),
        retry=(
            tenacity.retry_if_exception(hvac.exceptions.VaultNotInitialized) |
            tenacity.retry_if_exception(hvac.exceptions.VaultDown)
            )
        )
    def _do_it():
        client = _vault_client(config)
        func(args, client, config)
    _do_it()


def encrypt(args, config):
    """Encrypt and open handler

    :param: args: argparser generated cli arguments
    :param: config: configparser object of vaultlocker config
    """
    _do_it_with_persistence(_encrypt_block_device, args, config)


def decrypt(args, config):
    """Decrypt and open handler

    :param: args: argparser generated cli arguments
    :param: config: configparser object of vaultlocker config
    """
    _do_it_with_persistence(_decrypt_block_device, args, config)


def get_config(config_path):
    """Read vaultlocker configuration from config file

    :param: config_path: path to the configuration file
    :returns: configparser. Parsed configuration options
    """
    config = configparser.ConfigParser({'kv_version': '1'})
    if os.path.exists(config_path):
        config.read(config_path)
    else:
        raise FileNotFoundError(
            "Configuration file not found: {}".format(config_path)
        )
    return config


def main():
    parser = argparse.ArgumentParser('vaultlocker')
    parser.set_defaults(prog=parser.prog)
    subparsers = parser.add_subparsers(
        title="subcommands",
        description="valid subcommands",
        help="sub-command help",
    )
    parser.add_argument(
        '--retry',
        default=-1,
        type=int,
        help="Time in seconds to continue retrying to connect to Vault"
    )
    parser.add_argument(
        '--config',
        default=DEFAULT_CONF_FILE,
        type=str,
        help="Path to vaultlocker configuration file"
    )

    encrypt_parser = subparsers.add_parser(
        'encrypt',
        help='Encrypt a block device and store its key in Vault'
    )
    encrypt_parser.add_argument('--uuid',
                                dest="uuid",
                                help="UUID to use to reference encryption key")
    encrypt_parser.add_argument('block_device',
                                metavar='BLOCK_DEVICE', nargs=1,
                                help="Full path to block device to encrypt")
    encrypt_parser.set_defaults(func=encrypt)

    decrypt_parser = subparsers.add_parser(
        'decrypt',
        help='Decrypt a block device retrieving its key from Vault'
    )
    decrypt_parser.add_argument('uuid',
                                metavar='uuid', nargs=1,
                                help='UUID of block device to decrypt')
    decrypt_parser.set_defaults(func=decrypt)

    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG)

    try:
        args.func(args, get_config(args.config))
    except Exception as e:
        raise SystemExit(
            '{prog}: {msg}'.format(
                prog=args.prog,
                msg=e,
            )
        )

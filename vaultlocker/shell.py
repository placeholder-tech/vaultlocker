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


from __future__ import absolute_import

import argparse
import uuid
import hvac
import logging
import os
import socket
import shutil
import tenacity

from six.moves import configparser

from . import dmcrypt

logger = logging.getLogger(__name__)

RUN_VAULTLOCKER = '/run/vaultlocker'
CONF_FILE = '/etc/vaultlocker/vaultlocker.conf'


def _vault_client(config):
    """Helper wrapper to create Vault Client"""
    client = hvac.Client(url=config.get('vault', 'url'))
    client.auth_approle(config.get('vault', 'approle'))
    return client


def _get_vault_path(device_uuid, config):
    return '{}/{}/{}'.format(config.get('vault', 'backend'),
                             socket.gethostname(),
                             device_uuid)


def _store_file_in_vault(args, client, config):
    source = args.source[0]
    if not os.path.exists(source):
        raise ValueError('Unable to locate source file {}'.format(source))

    source_uuid = str(uuid.uuid4())
    logger.info('Storing secret {} in vault'.format(source_uuid))

    vault_path = _get_vault_path(source_uuid, config)

    with open(source, 'rb') as input_file:
        input_data = input_file.read()
        client.write(vault_path,
                     content=input_data,
                     source_path=source)
        stored_data = \
            client.read(vault_path)
        assert input_data == stored_data['data']['content']
        assert source == stored_data['data']['source_path']

    if not os.path.exists(RUN_VAULTLOCKER):
        os.makedirs(RUN_VAULTLOCKER)

    new_path = os.path.join(RUN_VAULTLOCKER, source_uuid)
    shutil.move(source, new_path)
    os.symlink(new_path, source)
    print(source_uuid)


def _retrieve_file_from_vault(args, client, config):
    target_uuid = args[0]
    new_path = os.path.join(RUN_VAULTLOCKER, target_uuid)
    if os.path.exists(new_path):
        logger.info('Secret {} already on disk, skipping'.format(target_uuid))
        return

    vault_path = _get_vault_path(target_uuid, config)

    logger.info('Retrieving secret {} from vault'.format(target_uuid))
    stored_file = client.read(vault_path)

    if not os.path.exists(RUN_VAULTLOCKER):
        os.makedirs(RUN_VAULTLOCKER)

    with open(new_path, 'wb') as target:
        os.fchmod(target.fileno(), 0o400)
        target.write(stored_file['data']['content'])

    original_source = stored_file['data']['source_path']
    if os.path.exists(original_source):
        os.remove(original_source)
    os.symlink(new_path, original_source)


def _encrypt_block_device(args, client, config):
    block_device = args.block_device[0]
    key = dmcrypt.generate_key()
    block_uuid = str(uuid.uuid4())
    vault_path = _get_vault_path(block_uuid, config)

    dmcrypt.luks_format(key, block_device, block_uuid)

    # NOTE: store and validate key
    client.write(vault_path,
                 dmcrypt_key=key)
    stored_data = client.read(vault_path)
    assert key == stored_data['data']['dmcrypt_key']

    dmcrypt.luks_open(key, block_uuid)


def _decrypt_block_device(args, client, config):
    block_uuid = args.uuid[0]
    vault_path = _get_vault_path(block_uuid, config)

    stored_data = client.read(vault_path)
    if stored_data is None:
        raise ValueError('Unable to locate key for {}'.format(block_uuid))
    key = stored_data['data']['dmcrypt_key']

    dmcrypt.luks_open(key, block_uuid)


def _do_it_with_persistence(func, args, config):
    @tenacity.retry(
        wait=tenacity.wait_fixed(1),
        stop=(tenacity.stop_after_delay(args.retry) if args.retry > 0
                else tenacity.stop_after_attempt(1)),
        retry=(tenacity.retry_if_exception(hvac.exceptions.VaultNotInitialized) |
               tenacity.retry_if_exception(hvac.exceptions.VaultDown)))
    def _do_it():
        client = _vault_client(config)
        func(args, client, config)
    _do_it()


def store(args, config):
    _do_it_with_persistence(_store_file_in_vault, args, config)


def retrieve(args, config):
    _do_it_with_persistence(_retrieve_file_from_vault, args, config)


def encrypt(args, config):
    _do_it_with_persistence(_encrypt_block_device, args, config)


def decrypt(args, config):
    _do_it_with_persistence(_decrypt_block_device, args, config)


def get_config():
    config = configparser.ConfigParser()
    if os.path.exists(CONF_FILE):
        config.read(CONF_FILE)
    return config


def main():
    parser = argparse.ArgumentParser('vaultlocker')
    parser.set_defaults(prog=parser.prog)
    subparsers = parser.add_subparsers(
        title="subcommands",
        description="valid subcommands",
        help="sub-command help",
    )
    parser.add_argument('--retry',
                        default=-1,
                        type=int,
                        help="Time in seconds to continue retrying to connect to Vault")

    store_parser = subparsers.add_parser('store', help='Store new file in Vault')
    store_parser.add_argument('source',
                              metavar='SOURCE', nargs=1)
    store_parser.set_defaults(func=store)


    retrieve_parser = subparsers.add_parser('retrieve', help='Retrieve file by UUID from Vault')
    retrieve_parser.add_argument('target_uuid',
                                 metavar='TARGET_UUID', nargs=1)
    retrieve_parser.set_defaults(func=retrieve)

    encrypt_parser = subparsers.add_parser('encrypt', help='Encrypt a block device and store its key in Vault')
    encrypt_parser.add_argument('block_device',
                                metavar='BLOCK_DEVICE', nargs=1,
                                help="Full path to block device to encrypt")
    encrypt_parser.set_defaults(func=encrypt)

    decrypt_parser = subparsers.add_parser('decrypt', help='Decrypt a block device retrieving its key from Vault')
    decrypt_parser.add_argument('uuid',
                                metavar='uuid', nargs=1,
                                help='UUID of block device to decrypt')
    decrypt_parser.set_defaults(func=decrypt)

    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG)

    try:
        args.func(args, get_config())
    except Exception as e:
        raise SystemExit(
            '{prog}: {msg}'.format(
                prog=args.prog,
                msg=e,
            )
        )

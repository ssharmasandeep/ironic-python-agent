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

import os
import re
import shutil
import tempfile

from ironic_lib import disk_utils
from oslo_concurrency import processutils
from oslo_log import log

from ironic_python_agent import errors
from ironic_python_agent import partition_utils
from ironic_python_agent import utils


LOG = log.getLogger(__name__)


def manage_uefi(device, efi_system_part_uuid=None):
    """Manage the device looking for valid efi bootloaders to update the nvram.

    This method checks for valid efi bootloaders in the device, if they exists
    it updates the nvram using the efibootmgr.

    :param device: the device to be checked.
    :param efi_system_part_uuid: efi partition uuid.
    :raises: DeviceNotFound if the efi partition cannot be found.
    :return: True - if it founds any efi bootloader and the nvram was updated
             using the efibootmgr.
             False - if no efi bootloader is found.
    """
    efi_partition_mount_point = None
    efi_mounted = False
    LOG.debug('Attempting UEFI loader autodetection and NVRAM record setup.')
    try:
        # Force UEFI to rescan the device.
        utils.rescan_device(device)

        local_path = tempfile.mkdtemp()
        # Trust the contents on the disk in the event of a whole disk image.
        efi_partition = disk_utils.find_efi_partition(device)
        if efi_partition:
            efi_partition = efi_partition['number']

        if not efi_partition and efi_system_part_uuid:
            # _get_partition returns <device>+<partition> and we only need the
            # partition number
            partition = partition_utils.get_partition(
                device, uuid=efi_system_part_uuid)
            try:
                efi_partition = int(partition.replace(device, ""))
            except ValueError:
                # NVMe Devices get a partitioning scheme that is different from
                # traditional block devices like SCSI/SATA
                efi_partition = int(partition.replace(device + 'p', ""))

        if not efi_partition:
            # NOTE(dtantsur): we cannot have a valid EFI deployment without an
            # EFI partition at all. This code path is easily hit when using an
            # image that is not UEFI compatible (which sadly applies to most
            # cloud images out there, with a nice exception of Ubuntu).
            raise errors.DeviceNotFound(
                "No EFI partition could be detected on device %s and "
                "EFI partition UUID has not been recorded during deployment "
                "(which is often the case for whole disk images). "
                "Are you using a UEFI-compatible image?" % device)

        efi_partition_mount_point = os.path.join(local_path, "boot/efi")
        if not os.path.exists(efi_partition_mount_point):
            os.makedirs(efi_partition_mount_point)

        # The mount needs the device with the partition, in case the
        # device ends with a digit we add a `p` and the partition number we
        # found, otherwise we just join the device and the partition number
        if device[-1].isdigit():
            efi_device_part = '{}p{}'.format(device, efi_partition)
            utils.execute('mount', efi_device_part, efi_partition_mount_point)
        else:
            efi_device_part = '{}{}'.format(device, efi_partition)
            utils.execute('mount', efi_device_part, efi_partition_mount_point)
        efi_mounted = True

        valid_efi_bootloaders = _get_efi_bootloaders(efi_partition_mount_point)
        if valid_efi_bootloaders:
            _run_efibootmgr(valid_efi_bootloaders, device, efi_partition,
                            efi_partition_mount_point)
            return True
        else:
            # NOTE(dtantsur): if we have an empty EFI partition, try to use
            # grub-install to populate it.
            LOG.warning('Empty EFI partition detected.')
            return False

    except processutils.ProcessExecutionError as e:
        error_msg = ('Could not verify uefi on device %(dev)s, '
                     'failed with %(err)s.' % {'dev': device, 'err': e})
        LOG.error(error_msg)
        raise errors.CommandExecutionError(error_msg)
    finally:
        LOG.debug('Executing _manage_uefi clean-up.')
        umount_warn_msg = "Unable to umount %(local_path)s. Error: %(error)s"

        try:
            if efi_mounted:
                utils.execute('umount', efi_partition_mount_point,
                              attempts=3, delay_on_retry=True)
        except processutils.ProcessExecutionError as e:
            error_msg = ('Umounting efi system partition failed. '
                         'Attempted 3 times. Error: %s' % e)
            LOG.error(error_msg)
            raise errors.CommandExecutionError(error_msg)

        else:
            # If umounting the binds succeed then we can try to delete it
            try:
                utils.execute('sync')
            except processutils.ProcessExecutionError as e:
                LOG.warning(umount_warn_msg, {'path': local_path, 'error': e})
            else:
                # After everything is umounted we can then remove the
                # temporary directory
                shutil.rmtree(local_path)


# NOTE(TheJulia): Do not add bootia32.csv to this list. That is 32bit
# EFI booting and never really became popular.
BOOTLOADERS_EFI = [
    'bootx64.csv',  # Used by GRUB2 shim loader (Ubuntu, Red Hat)
    'boot.csv',  # Used by rEFInd, Centos7 Grub2
    'bootia32.efi',
    'bootx64.efi',  # x86_64 Default
    'bootia64.efi',
    'bootarm.efi',
    'bootaa64.efi',  # Arm64 Default
    'bootriscv32.efi',
    'bootriscv64.efi',
    'bootriscv128.efi',
    'grubaa64.efi',
    'winload.efi'
]


def _get_efi_bootloaders(location):
    """Get all valid efi bootloaders in a given location

    :param location: the location where it should start looking for the
                     efi files.
    :return: a list of relative paths to valid efi bootloaders or reference
             files.
    """
    # Let's find all files with .efi or .EFI extension
    LOG.debug('Looking for all efi files on %s', location)
    valid_bootloaders = []
    for root, dirs, files in os.walk(location):
        efi_files = [f for f in files if f.lower() in BOOTLOADERS_EFI]
        LOG.debug('efi files found in %(location)s : %(efi_files)s',
                  {'location': location, 'efi_files': str(efi_files)})
        for name in efi_files:
            efi_f = os.path.join(root, name)
            LOG.debug('Checking if %s is executable', efi_f)
            if os.access(efi_f, os.X_OK):
                v_bl = efi_f.split(location)[-1][1:]
                LOG.debug('%s is a valid bootloader', v_bl)
                valid_bootloaders.append(v_bl)
            if 'csv' in efi_f.lower():
                v_bl = efi_f.split(location)[-1][1:]
                LOG.debug('%s is a pointer to a bootloader', v_bl)
                # The CSV files are intended to be authortative as
                # to the bootloader and the label to be used. Since
                # we found one, we're going to point directly to it.
                # centos7 did ship with 2, but with the same contents.
                # TODO(TheJulia): Perhaps we extend this to make a list
                # of CSVs instead and only return those?! But then the
                # question is which is right/first/preferred.
                return [v_bl]
    return valid_bootloaders


def _run_efibootmgr(valid_efi_bootloaders, device, efi_partition,
                    mount_point):
    """Executes efibootmgr and removes duplicate entries.

    :param valid_efi_bootloaders: the list of valid efi bootloaders
    :param device: the device to be used
    :param efi_partition: the efi partition on the device
    :param mount_point: The mountpoint for the EFI partition so we can
                        read contents of files if necessary to perform
                        proper bootloader injection operations.
    """

    # Before updating let's get information about the bootorder
    LOG.debug("Getting information about boot order.")
    original_efi_output = utils.execute('efibootmgr', '-v')
    # NOTE(TheJulia): regex used to identify entries in the efibootmgr
    # output on stdout.
    entry_label = re.compile(r'Boot([0-9a-f-A-F]+)\*?\s(.*).*$')
    label_id = 1
    for v_bl in valid_efi_bootloaders:
        if 'csv' in v_bl.lower():
            LOG.debug('A CSV file has been identified as a bootloader hint. '
                      'File: %s', v_bl)
            # These files are always UTF-16 encoded, sometimes have a header.
            # Positive bonus is python silently drops the FEFF header.
            with open(mount_point + '/' + v_bl, 'r', encoding='utf-16') as csv:
                contents = str(csv.read())
            csv_contents = contents.split(',', maxsplit=3)
            csv_filename = v_bl.split('/')[-1]
            v_efi_bl_path = v_bl.replace(csv_filename, str(csv_contents[0]))
            v_efi_bl_path = '\\' + v_efi_bl_path.replace('/', '\\')
            label = csv_contents[1]
        else:
            v_efi_bl_path = '\\' + v_bl.replace('/', '\\')
            label = 'ironic' + str(label_id)

        # Iterate through standard out, and look for duplicates
        for line in original_efi_output[0].split('\n'):
            match = entry_label.match(line)
            # Look for the base label in the string if a line match
            # occurs, so we can identify if we need to eliminate the
            # entry.
            if match and label in match.group(2):
                boot_num = match.group(1)
                LOG.debug("Found bootnum %s matching label", boot_num)
                utils.execute('efibootmgr', '-b', boot_num, '-B')

        LOG.debug("Adding loader %(path)s on partition %(part)s of device "
                  " %(dev)s", {'path': v_efi_bl_path, 'part': efi_partition,
                               'dev': device})
        # Update the nvram using efibootmgr
        # https://linux.die.net/man/8/efibootmgr
        utils.execute('efibootmgr', '-v', '-c', '-d', device,
                      '-p', efi_partition, '-w', '-L', label,
                      '-l', v_efi_bl_path)
        # Increment the ID in case the loop runs again.
        label_id += 1
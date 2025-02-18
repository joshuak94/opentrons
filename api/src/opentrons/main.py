import os
import logging
import asyncio
import re
from opentrons import HERE
from opentrons import server
from opentrons.server.main import build_arg_parser
from argparse import ArgumentParser
from opentrons import hardware, __version__
from opentrons.config import feature_flags as ff, name, robot_configs
from opentrons.system import udev, resin
from opentrons.util import logging_config
from opentrons.drivers.smoothie_drivers.driver_3_0 import SmoothieDriver_3_0_0

try:
    from opentrons.hardware_control.socket_server\
        import run as install_hardware_server
except ImportError:
    async def install_hardware_server(sock_path, api):  # type: ignore
        log.warning("Cannot start hardware server: missing dependency")


log = logging.getLogger(__name__)


def _find_smoothie_file():
    resources = os.listdir(os.path.join(HERE, 'resources'))
    for fi in resources:
        matches = re.search('smoothie-(.*).hex', fi)
        if matches:
            branch_plus_ref = matches.group(1)
            return os.path.join(HERE, 'resources', fi), branch_plus_ref
    raise OSError("Could not find smoothie firmware file in {}"
                  .format(os.path.join(HERE, 'resources')))


async def _do_fw_update(new_fw_path, new_fw_ver):
    """ Update the connected smoothie board, with retries

    When the API server boots, it talks to the motor controller board for the
    first time. Sometimes the board is in a bad state - it might have the
    wrong firmware version (i.e. this is the first boot after an update), or it
    might just not be communicating correctly. Sometimes, the motor controller
    not communicating correctly in fact means it needs a firmware update; other
    times, it might mean it just needs to be reset.

    This function is called when the API server boots if either of the above
    cases happens. Its job is to make the motor controller board ready by
    updating its firmware, regardless of the state of the rest of the stack.

    To that end, this function uses the smoothie driver directly (so it can
    ignore the rest of the stack) and has a couple retries with different
    hardware line changes in between (so it can catch all failure modes). If
    this method ultimately fails, it lets the server boot by telling it to
    consider itself virtual.

    After this function has completed, it is always safe to call
    hardware.connect() - it just might be virtual
    """
    explicit_modeset = False
    driver = SmoothieDriver_3_0_0(robot_configs.load())
    for attempts in range(3):
        try:
            await driver.update_firmware(
                new_fw_path,
                explicit_modeset=explicit_modeset)
        except RuntimeError:
            explicit_modeset = True
            continue

        if driver.get_fw_version() == new_fw_ver:
            log.info(f"Smoothie fw update complete in {attempts} tries")
            break
        else:
            log.error(
                "Failed to update smoothie: did not connect after update")
    else:
        log.error("Could not update smoothie, forcing virtual")
        os.environ['ENABLE_VIRTUAL_SMOOTHIE'] = 'true'


def initialize_robot(loop):
    packed_smoothie_fw_file, packed_smoothie_fw_ver = _find_smoothie_file()
    try:
        hardware.connect()
    except Exception as e:
        # The most common reason for this exception (aside from hardware
        # failures such as a disconnected smoothie) is that the smoothie
        # is in programming mode. If it is, then we still want to update
        # it (so it can boot again), but we don’t have to do the GPIO
        # manipulations that _put_ it in programming mode
        log.exception("Error while connecting to motor driver: {}".format(e))
        fw_version = None
    else:
        if ff.use_protocol_api_v2():
            fw_version = loop.run_until_complete(hardware.fw_version)
        else:
            fw_version = hardware.fw_version
    log.info("Smoothie FW version: {}".format(fw_version))
    if fw_version != packed_smoothie_fw_ver:
        log.info("Executing smoothie update: current vers {}, packed vers {}"
                 .format(fw_version, packed_smoothie_fw_ver))
        loop.run_until_complete(
            _do_fw_update(packed_smoothie_fw_file, packed_smoothie_fw_ver))
        hardware.connect()
    else:
        log.info("FW version OK: {}".format(packed_smoothie_fw_ver))
    log.info(f"Name: {name()}")


def run(**kwargs):  # noqa(C901)
    """
    This function was necessary to separate from main() to accommodate for
    server startup path on system 3.0, which is server.main. In the case where
    the api is on system 3.0, server.main will redirect to this function with
    an additional argument of 'patch_old_init'. kwargs are hence used to allow
    the use of different length args
    """
    loop = asyncio.get_event_loop()
    if ff.use_protocol_api_v2():
        robot_conf = loop.run_until_complete(hardware.config)
    else:
        robot_conf = hardware.config

    logging_config.log_init(robot_conf.log_level)

    log.info("API server version:  {}".format(__version__))
    if not os.environ.get("ENABLE_VIRTUAL_SMOOTHIE"):
        initialize_robot(loop)
        if ff.use_protocol_api_v2():
            loop.run_until_complete(hardware.cache_instruments())
        if not ff.disable_home_on_boot():
            log.info("Homing Z axes")
            if ff.use_protocol_api_v2():
                loop.run_until_complete(hardware.home_z())
            else:
                hardware.home_z()
        try:
            udev.setup_rules_file()
        except Exception:
            log.exception(
                "Could not setup udev rules, modules may not be detected")
    # Explicitly unlock resin updates in case a prior server left them locked
    resin.unlock_updates()
    if kwargs.get('hardware_server'):
        if ff.use_protocol_api_v2():
            loop.run_until_complete(
                install_hardware_server(kwargs['hardware_server_socket'],
                                        hardware._api))
        else:
            log.warning(
                "Hardware server requested but apiv1 selected, not starting")
    server.run(kwargs.get('hostname'), kwargs.get('port'), kwargs.get('path'),
               loop)


def main():
    """ The main entrypoint for the Opentrons robot API server stack.

    This function
    - creates and starts the server for both the RPC routes
      handled by :py:mod:`opentrons.server.rpc` and the HTTP routes handled
      by :py:mod:`opentrons.server.http`
    - initializes the hardware interaction handled by either
      :py:mod:`opentrons.legacy_api` or :py:mod:`opentrons.hardware_control`

    This function does not return until the server is brought down.
    """

    arg_parser = ArgumentParser(
        description="Opentrons robot software",
        parents=[build_arg_parser()])
    arg_parser.add_argument(
        '--hardware-server', action='store_true',
        help='Run a jsonrpc server allowing rpc to the'
        ' hardware controller. Only works on buildroot '
        'because extra dependencies are required.')
    arg_parser.add_argument(
        '--hardware-server-socket', action='store',
        default='/var/run/opentrons-hardware.sock',
        help='Override for the hardware server socket')
    args = arg_parser.parse_args()
    run(**vars(args))
    arg_parser.exit(message="Stopped\n")


if __name__ == "__main__":
    main()

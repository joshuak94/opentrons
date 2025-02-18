from opentrons.drivers.mag_deck import MagDeck as MagDeckDriver
from opentrons import commands

LABWARE_ENGAGE_HEIGHT = {
    'biorad-hardshell-96-PCR': 18,
    'biorad_96_wellplate_200ul_pcr': 18,
    'nest_96_wellplate_100ul_pcr_full_skirt': 20}    # mm
MAX_ENGAGE_HEIGHT = 45  # mm from home position


class MissingDevicePortError(Exception):
    pass


# TODO: BC 2018-08-03 this class shares a fair amount verbatim from TempDeck,
# there should be an upstream ABC in the future to contain shared logic
# between modules
class MagDeck(commands.CommandPublisher):
    '''
    Under development. API subject to change
    '''
    def __init__(self, lw=None, port=None, broker=None):
        super().__init__(broker)
        self.labware = lw
        self._port = port
        self._engaged = False
        self._driver = None
        self._device_info = None

    @commands.publish.both(command=commands.magdeck_calibrate)
    def calibrate(self):
        '''
        Calibration involves probing for top plate to get the plate height
        '''
        if self._driver and self._driver.is_connected():
            self._driver.probe_plate()
            # return if successful or not?
            self._engaged = False

    @commands.publish.both(command=commands.magdeck_engage)
    def engage(self, **kwargs):
        '''
        Move the magnet to either:
            the default height for the labware loaded on magdeck
            [engage()]
        or  a +/- 'offset' from the default height for the labware
            [engage(offset=2)]
        or  a 'height' value specified as mm from magdeck home position
            [engage(height=20)]
        '''
        if 'height' in kwargs:
            height = kwargs.get('height')
        else:
            height = LABWARE_ENGAGE_HEIGHT.get(
                self.labware.get_children_list()[1].get_name())
            if not height:
                raise ValueError(
                    'No engage height definition found for {}. Provide a'
                    'custom height instead'.format(
                        self.labware.get_children_list()[1].get_name()))
            if 'offset' in kwargs:
                height += kwargs.get('offset')
        if height > MAX_ENGAGE_HEIGHT or height < 0:
            raise ValueError('Invalid engage height. Should be 0 to {}'.format(
                MAX_ENGAGE_HEIGHT))
        if self._driver and self._driver.is_connected():
            self._driver.move(height)
            self._engaged = True

    @commands.publish.both(command=commands.magdeck_disengage)
    def disengage(self):
        '''
        Home the magnet
        '''
        if self._driver and self._driver.is_connected():
            self._driver.home()
            self._engaged = False

    @classmethod
    def name(cls):
        return 'magdeck'

    @classmethod
    def display_name(cls):
        return 'Magnetic Deck'

    # TODO: there should be a separate decoupled set of classes that
    # construct the http api response entity given the model instance.
    def to_dict(self):
        return {
            'name': self.name(),
            'port': self.port,
            'serial': self.device_info and self.device_info.get('serial'),
            'model': self.device_info and self.device_info.get('model'),
            'fwVersion': self.device_info and self.device_info.get('version'),
            'displayName': self.display_name(),
            **self.live_data
        }

    @property
    def live_data(self):
        return {
            'status': self.status,
            'data': {
                'engaged': self._engaged
            }
        }

    @property
    def port(self):
        """ Serial Port """
        return self._port

    @property
    def device_info(self):
        """
        Returns a dict:
            { 'serial': 'abc123', 'model': '8675309', 'version': '9001' }
        """
        return self._device_info

    @property
    def status(self):
        return 'engaged' if self._engaged else 'disengaged'

    # Internal Methods

    def connect(self):
        '''
        Connect to the serial port
        '''
        if self._port:
            self._driver = MagDeckDriver()
            self._driver.connect(self._port)
            self._device_info = self._driver.get_device_info()
        else:
            # Sanity check: Should never happen, because connect should
            # never be called without a port on Module
            raise MissingDevicePortError(
                "MagDeck couldnt connect to port {}".format(self._port)
            )

    def disconnect(self):
        '''
        Disconnect from the serial port
        '''
        if self._driver:
            self._driver.disconnect()

from collections import UserDict
import functools
import logging
import pkgutil
import json
from typing import Any, List, Optional, Tuple, Union, Dict

from opentrons import types
from .labware import (Labware, Well, ModuleGeometry,
                      quirks_from_any_parent, ThermocyclerGeometry)
from opentrons.hardware_control.types import CriticalPoint


MODULE_LOG = logging.getLogger(__name__)


def max_many(*args):
    return functools.reduce(max, args[1:], args[0])


def plan_moves(
        from_loc: types.Location,
        to_loc: types.Location,
        deck: 'Deck',
        well_z_margin: float = 5.0,
        lw_z_margin: float = 10.0,
        force_direct: bool = False,
        minimum_z_height: float = None)\
        -> List[Tuple[types.Point,
                      Optional[CriticalPoint]]]:
    """ Plan moves between one :py:class:`.Location` and another.

    Each :py:class:`.Location` instance might or might not have a specific
    kind of geometry attached. This function is intended to return series
    of moves that contain the minimum safe retractions to avoid (known)
    labware on the specified :py:class:`Deck`.

    :param from_loc: The last location.
    :param to_loc: The location to move to.
    :param deck: The :py:class:`Deck` instance describing the robot.
    :param float well_z_margin: How much extra Z margin to raise the cp by over
                                the bare minimum to clear wells within the same
                                labware. Default: 5mm
    :param float lw_z_margin: How much extra Z margin to raise the cp by over
                              the bare minimum to clear different pieces of
                              labware. Default: 20mm
    :param force_direct: If True, ignore any Z margins force a direct move
    :param minimum_z_height: When specified, this Z margin is able to raise
                             (but never lower) the mid-arc height.

    :returns: A list of tuples of :py:class:`.Point` and critical point
              overrides to move through.
    """

    assert minimum_z_height is None or minimum_z_height >= 0.0

    def _split_loc_labware(
            loc: types.Location) -> Tuple[Optional[Labware], Optional[Well]]:
        if isinstance(loc.labware, Labware):
            return loc.labware, None
        elif isinstance(loc.labware, Well):
            return loc.labware.parent, loc.labware
        else:
            return None, None

    to_point = to_loc.point
    to_lw, to_well = _split_loc_labware(to_loc)
    from_point = from_loc.point
    from_lw, from_well = _split_loc_labware(from_loc)
    dest_quirks = quirks_from_any_parent(to_lw)
    from_quirks = quirks_from_any_parent(from_lw)
    from_center = 'centerMultichannelOnWells' in from_quirks
    to_center = 'centerMultichannelOnWells' in dest_quirks
    dest_cp_override = CriticalPoint.XY_CENTER if to_center else None
    origin_cp_override = CriticalPoint.XY_CENTER if from_center else None

    is_same_location = ((to_lw and to_lw == from_lw)
                        and (to_well and to_well == from_well))
    if (force_direct or (is_same_location and not
                         (minimum_z_height or 0) > 0)):
        # If we’re going direct, we can assume we’re already in the correct
        # cp so we can use the override without prep
        return [(to_point, dest_cp_override)]

    # Generate arc moves

    # Find the safe z heights based on the destination and origin labware/well
    if to_lw and to_lw == from_lw:
        # If we know the labwares we’re moving from and to, we can calculate
        # a safe z based on their heights
        if to_well:
            to_safety = to_well.top().point.z + well_z_margin
        else:
            to_safety = to_lw.highest_z + well_z_margin
        if from_well:
            from_safety = from_well.top().point.z + well_z_margin
        else:
            from_safety = from_lw.highest_z + well_z_margin
    else:
        # One of our labwares is invalid so we have to just go above
        # deck.highest_z since we don’t know where we are
        to_safety = deck.highest_z + lw_z_margin
        from_safety = 0.0  # (ignore since it’s in a max())

    safe = max_many(
        to_point.z,
        from_point.z,
        to_safety,
        from_safety,
        minimum_z_height or 0)

    # We should use the origin’s cp for the first move since it should
    # move only in z and the destination’s cp subsequently
    return [(from_point._replace(z=safe), origin_cp_override),
            (to_point._replace(z=safe), dest_cp_override),
            (to_point, dest_cp_override)]


DeckItem = Union[Labware, ModuleGeometry, ThermocyclerGeometry]


class Deck(UserDict):
    def __init__(self):
        super().__init__()
        row_offset = 90.5
        col_offset = 132.5
        for idx in range(1, 13):
            self.data[idx] = None
        self._positions = {idx + 1: types.Point((idx % 3) * col_offset,
                                                idx // 3 * row_offset,
                                                0)
                           for idx in range(12)}
        self._highest_z = 0.0
        # TODO: support deck loadName as a param
        def_path = 'shared_data/deck/definitions/1/ot2_standard.json'
        self._definition = json.loads(  # type: ignore
            pkgutil.get_data('opentrons', def_path))

    @staticmethod
    def _assure_int(key: object) -> int:
        if isinstance(key, str):
            return int(key)
        elif isinstance(key, int):
            return key
        else:
            raise TypeError(type(key))

    def _check_name(self, key: object) -> int:
        should_raise = False
        try:
            key_int = Deck._assure_int(key)
        except Exception:
            MODULE_LOG.exception("Bad slot name: {}".format(key))
            should_raise = True
        should_raise = should_raise or key_int not in self.data
        if should_raise:
            raise ValueError("Unknown slot: {}".format(key))
        else:
            return key_int

    def __getitem__(self, key: types.DeckLocation) -> DeckItem:
        return self.data[self._check_name(key)]

    def __delitem__(self, key: types.DeckLocation) -> None:
        checked_key = self._check_name(key)
        old = self.data[checked_key]
        self.data[checked_key] = None
        if old:
            self.recalculate_high_z()

    def __setitem__(self, key: types.DeckLocation, val: DeckItem) -> None:
        key_int = self._check_name(key)
        labware = self.data.get(key_int)
        if labware is not None:
            if key_int == 12:
                if 'fixedTrash' in labware.parameters.get('quirks', []):
                    pass
                else:
                    raise ValueError('Deck location {} is for fixed trash only'
                                     .format(key))
            else:
                raise ValueError('Deck location {} already has an item: {}'
                                 .format(key, self.data[key_int]))
        self.data[key_int] = val
        self._highest_z = max(val.highest_z, self._highest_z)

    def __contains__(self, key: object) -> bool:
        try:
            key_int = self._check_name(key)
        except ValueError:
            return False
        return key_int in self.data

    def position_for(self, key: types.DeckLocation) -> types.Location:
        key_int = self._check_name(key)
        return types.Location(self._positions[key_int], str(key))

    def recalculate_high_z(self):
        self._highest_z = 0.0
        for item in [lw for lw in self.data.values() if lw]:
            self._highest_z = max(item.highest_z, self._highest_z)

    def get_slot_definition(self, slot_name) -> Dict[str, Any]:
        slots: List[Dict] = self._definition['locations']['orderedSlots']
        slot_def = next(
            (slot for slot in slots if slot['id'] == slot_name), None)
        if not slot_def:
            slot_ids = [slot['id'] for slot in slots]
            raise ValueError(f'slot {slot_name} could not be found,'
                             f'valid deck slots are: {slot_ids}')
        return slot_def

    def resolve_module_location(
            self, module_name: str,
            location: Optional[types.DeckLocation]) -> types.DeckLocation:
        if isinstance(location, str) or isinstance(location, int):
            slot_def: Dict[str, Any] = self.get_slot_definition(
                str(location))
            compatible_modules: List[str] = slot_def['compatibleModules']
            if module_name in compatible_modules:
                return location
            else:
                raise AssertionError(
                    f'module {module_name} cannot be loaded'
                    ' into slot {location}')
        else:
            valid_slots = [slot['id'] for slot in self.slots if module_name
                           in slot['compatibleModules']]
            if len(valid_slots) == 1:
                return valid_slots[0]
            else:
                raise AssertionError(
                    f'module {module_name} does not have a default'
                    ' location, you must specify a slot')

    @property
    def highest_z(self) -> float:
        """ Return the tallest known point on the deck. """
        return self._highest_z

    @property
    def slots(self) -> List[Dict]:
        """ Return the definition of the loaded robot deck. """
        return self._definition['locations']['orderedSlots']

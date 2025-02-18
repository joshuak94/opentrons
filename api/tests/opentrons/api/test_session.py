import itertools

import pytest
import ast

from opentrons.api.session import (
    _accumulate, _get_labware, _dedupe, extract_metadata, infer_version)
from tests.opentrons.conftest import state
from opentrons.legacy_api.robot.robot import Robot
from functools import partial

state = partial(state, 'session')


@pytest.fixture
def run_session(request, session_manager):
    if not isinstance(session_manager._hardware, Robot):
        pytest.skip('requires api1 only')
        return None
    return session_manager.create('dino', 'from opentrons import robot')


@pytest.fixture
def labware_setup(hardware):
    from opentrons import containers, instruments

    tip_racks = \
        [containers.load('opentrons-tiprack-300ul', slot, slot)
         for slot in ['1', '4']]
    plates = \
        [containers.load('96-flat', slot, slot) for slot in ['2', '5']]

    p50 = instruments.P50_Multi(
        mount='right', tip_racks=tip_racks)

    p1000 = instruments.P1000_Single(
        mount='left', tip_racks=tip_racks)

    commands = [
        {
            'location': plates[0][0],
            'instrument': p50
        },
        {
            'location': plates[1]
        },
        {
            'locations': [plates[0][0], plates[1]],
            'instrument': p1000
        }
    ]

    return (p50, p1000), tip_racks, plates, commands


@pytest.mark.api1_only
async def test_load_from_text(session_manager, protocol):
    session = session_manager.create(name='<blank>', text=protocol.text)
    assert session.name == '<blank>'

    acc = []

    def traverse(commands):
        for command in commands:
            acc.append(command)
            traverse(command['children'])
    traverse(session.commands)
    # Less commands now that trash is built in
    assert len(acc) == 75


@pytest.mark.api1_only
async def test_clear_tips(session_manager, tip_clear_protocol):
    session = session_manager.create(
        name='<blank>', text=tip_clear_protocol.text)

    assert len(session._instruments) == 1
    for instrument in session._instruments:
        assert not instrument.tip_attached


async def test_async_notifications(main_router):
    main_router.broker.publish(
        'session', {'name': 'foo', 'payload': {'bar': 'baz'}})
    # Get async iterator
    aiter = main_router.notifications.__aiter__()
    # Then read the first item
    res = await aiter.__anext__()
    assert res == {'name': 'foo', 'payload': {'bar': 'baz'}}


def test_load_protocol_with_error(session_manager):
    with pytest.raises(Exception) as e:
        session = session_manager.create(name='<blank>', text='blah')
        assert session is None

    args, = e.value.args
    assert args == "name 'blah' is not defined"


@pytest.mark.api2_only
@pytest.mark.parametrize('protocol_file', ['testosaur_v2.py'])
async def test_load_and_run_v2(
            main_router,
            protocol,
            protocol_file,
            loop
        ):
    session = main_router.session_manager.create(
        name='<blank>',
        text=protocol.text)
    assert main_router.notifications.queue.qsize() == 1
    assert session.state == 'loaded'
    assert session.command_log == {}

    def run():
        session.run()

    await loop.run_in_executor(executor=None, func=run)
    assert len(session.command_log) == 4

    res = []
    index = 0
    async for notification in main_router.notifications:
        payload = notification['payload']
        index += 1  # Command log in sync with add-command events emitted
        if type(payload) is dict:
            state = payload.get('state')
        else:
            state = payload.state
        res.append(state)
        if state == 'finished':
            break

    assert [key for key, _ in itertools.groupby(res)] == \
        ['loaded', 'running', 'finished']
    assert main_router.notifications.queue.qsize() == 0,\
        'Notification should be empty after receiving "finished" event'
    session.run()
    assert len(session.command_log) == 4, \
        "Clears command log on the next run"


@pytest.mark.api1_only
@pytest.mark.parametrize('protocol_file', ['testosaur.py'])
async def test_load_and_run(
            main_router,
            protocol,
            protocol_file,
            loop
        ):
    session = main_router.session_manager.create(
        name='<blank>',
        text=protocol.text)
    assert main_router.notifications.queue.qsize() == 1
    assert session.state == 'loaded'
    assert session.command_log == {}
    main_router.calibration_manager.tip_probe(session.instruments[0])

    def run():
        session.run()

    await loop.run_in_executor(executor=None,
                               func=run)
    assert len(session.command_log) == 6

    res = []
    index = 0
    async for notification in main_router.notifications:
        payload = notification['payload']
        index += 1  # Command log in sync with add-command events emitted
        if type(payload) is dict:
            state = payload.get('state')
        else:
            state = payload.state
        res.append(state)
        if state == 'finished':
            break

    assert [key for key, _ in itertools.groupby(res)] == \
        ['loaded', 'probing', 'moving', 'ready', 'running', 'finished']
    assert main_router.notifications.queue.qsize() == 0,\
        'Notification should be empty after receiving "finished" state change'
    session.run()
    assert len(session.command_log) == 6, \
        "Clears command log on the next run"


def test_init(run_session):
    assert run_session.state == 'loaded'
    assert run_session.name == 'dino'


def test_set_state(run_session):
    states = 'loaded', 'running', 'finished', 'stopped', 'paused'
    for state in states:
        run_session.set_state(state)
        assert run_session.state == state

    with pytest.raises(ValueError):
        run_session.set_state('impossible-state')


def test_error_append(run_session):
    foo = Exception('Foo')
    bar = Exception('Bar')
    run_session.error_append(foo)
    run_session.error_append(bar)

    errors = [
        value
        for value in run_session.errors
        if isinstance(value.pop('timestamp'), int)
    ]

    assert errors == [
        {'error': foo},
        {'error': bar}
    ]


@pytest.mark.api1_only
async def test_get_instruments_and_containers(labware_setup,
                                              virtual_smoothie_env,
                                              loop,
                                              session_manager):
    instruments, tip_racks, plates, commands = labware_setup
    p50, p1000 = instruments

    instruments, containers, modules, interactions = \
        _accumulate([_get_labware(command) for command in commands])

    session = session_manager.create(name='', text='')
    # We are calling dedupe directly for testing purposes.
    # Normally it is called from within a session
    session._instruments.extend(_dedupe(instruments))
    session._containers.extend(_dedupe(containers))
    session._modules.extend(_dedupe(modules))
    session._interactions.extend(_dedupe(interactions))

    instruments = session.get_instruments()
    containers = session.get_containers()
    modules = session.get_modules()

    assert [i.name for i in instruments] == ['p50_multi_v1', 'p1000_single_v1']
    assert [i.id for i in instruments] == [id(p50), id(p1000)]
    assert [[t.slot for t in i.tip_racks] for i in instruments] == \
        [['1', '4'], ['1', '4']]
    assert [[c.slot for c in i.containers] for i in instruments] == \
        [['2'], ['2', '5']]

    assert [c.slot for c in containers] == ['2', '5']
    assert [[i.id for i in c.instruments] for c in containers] == \
        [[id(p50), id(p1000)], [id(p1000)]]
    assert [c.id for c in containers] == [id(plates[0]), id(plates[1])]

    # TODO(ben 20180717): Add meaningful data and assertions for modules once
    # TODO                the API object is in place
    assert modules == []


def test_accumulate():
    res = \
        _accumulate([
            (['a'], ['d'], ['g', 'h']),
            (['b', 'c'], ['e', 'f'], ['i'])
        ])

    assert res == (['a', 'b', 'c'], ['d', 'e', 'f'], ['g', 'h', 'i'])
    assert _accumulate([]) == ([], [], [], [])


def test_dedupe():
    assert ''.join(_dedupe('aaaaabbbbcbbbbcccaa')) == 'abc'


@pytest.mark.api1_only
def test_get_labware(labware_setup):
    instruments, tip_racks, plates, commands = labware_setup
    p100, p1000 = instruments

    assert _get_labware(commands[0]) == \
        ([p100], [plates[0]], [], [(p100, plates[0])])

    assert _get_labware(commands[1]) == \
        ([], [plates[1]], [], [])

    assert _get_labware(commands[2]) == \
        ([p1000],
         [plates[0], plates[1]],
         [],
         [(p1000, plates[0]), (p1000, plates[1])])

    instruments, containers, modules, interactions = \
        _accumulate([_get_labware(command) for command in commands])

    assert \
        [
            list(_dedupe(instruments)),
            list(_dedupe(containers)),
            list(_dedupe(modules)),
            list(_dedupe(interactions))
        ] == \
        [
            [p100, p1000],
            [plates[0], plates[1]],
            [],
            [(p100, plates[0]), (p1000, plates[0]), (p1000, plates[1])]
        ]


@pytest.mark.api1_only
async def test_session_model_functional(session_manager, protocol):
    session = session_manager.create(name='<blank>', text=protocol.text)
    assert [container.name for container in session.containers] == \
           ['tiprack', 'trough', 'plate', 'tall-fixed-trash']
    names = [instrument.name for instrument in session.instruments]
    assert names == ['p300_single_v1']


# TODO(artyom 20171018): design a small protocol specifically for the test
@pytest.mark.api1_only
@pytest.mark.parametrize('protocol_file', ['bradford_assay.py'])
async def test_drop_tip_with_trash(session_manager, protocol, protocol_file):
    """
    Bradford Assay is using drop_tip() with no arguments that assumes
    tip drop into trash-box. In this test we are confirming that
    that trash location is being inferred from a command, and trash
    is listed as a container for a protocol, as well as a container
    instruments are interacting with.
    """
    session = session_manager.create(name='<blank>', text=protocol.text)

    assert 'tall-fixed-trash' in [c.name for c in session.get_containers()]
    containers = sum([i.containers for i in session.get_instruments()], [])
    assert 'tall-fixed-trash' in [c.name for c in containers]


@pytest.mark.api1_only
async def test_session_create_error(main_router):
    with pytest.raises(SyntaxError):
        main_router.session_manager.create(
            name='<blank>',
            text='syntax error ;(')

    with pytest.raises(TimeoutError):
        # No state change is expected
        await main_router.wait_until(lambda _: True)

    with pytest.raises(ZeroDivisionError):
        main_router.session_manager.create(
            name='<blank>',
            text='1/0')

    with pytest.raises(TimeoutError):
        # No state change is expected
        await main_router.wait_until(lambda _: True)


def test_extract_metadata():
    expected = {
        'hello': 'world',
        'what?': 'no'
    }

    prot = """
this = 0
that = 1
metadata = {
'what?': 'no',
'hello': 'world'
}
fakedata = {
'who?': 'me',
'what?': 'green eggs'
}
print('wat?')
metadata['hello'] = 'moon'
fakedata['what?'] = 'ham'
"""

    parsed = ast.parse(prot, filename='testy', mode='exec')
    metadata = extract_metadata(parsed)
    assert metadata == expected


async def test_session_metadata(main_router):
    expected = {
        'hello': 'world',
        'what?': 'no'
    }

    prot = """
this = 0
that = 1
metadata = {
'what?': 'no',
'hello': 'world'
}
print('wat?')

def run(ctx):
    print('hi there')
"""

    session = main_router.session_manager.create(
        name='<blank>',
        text=prot)
    assert session.metadata == expected


def test_infer_version():
    prot_api1_no_meta_a = """
from opentrons import instruments

p = instruments.P10_Single(mount='right')
"""

    prot_api1_no_meta_b = """
import opentrons.instruments

p = instruments.P10_Single(mount='right')
"""

    prot_api1_no_meta_c = """
from opentrons import instruments as instr

p = instr.P10_Single(mount='right')
"""

    prot_api1_meta1 = """
from opentrons import instruments

metadata = {
  'apiLevel': '1'
  }

p = instruments.P10_Single(mount='right')
"""

    prot_api1_meta2 = """
from opentrons import instruments

metadata = {
  'apiLevel': '2'
  }

p = instruments.P10_Single(mount='right')
"""

    prot_api2_no_meta = """
from opentrons import types

def run(ctx):
    right = ctx.load_instrument('p300_single', types.Mount.RIGHT)
"""

    prot_api2_meta1 = """
from opentrons import types

metadata = {
  'apiLevel': '1'
  }

def run(ctx):
    right = ctx.load_instrument('p300_single', types.Mount.RIGHT)
"""

    prot_api2_meta2 = """
from opentrons import types

metadata = {
  'apiLevel': '2'
  }

def run(ctx):
    right = ctx.load_instrument('p300_single', types.Mount.RIGHT)
"""

    expected = {
        prot_api1_no_meta_a: '1',
        prot_api1_no_meta_b: '1',
        prot_api1_no_meta_c: '1',
        prot_api1_meta1: '1',
        prot_api1_meta2: '2',
        prot_api2_no_meta: '2',
        prot_api2_meta1: '1',
        prot_api2_meta2: '2'
    }

    def check(prot):
        parsed = ast.parse(prot, filename='test', mode='exec')
        metadata = extract_metadata(parsed)
        return infer_version(metadata, parsed)

    assert check(prot_api1_no_meta_a) == expected[prot_api1_no_meta_a]
    assert check(prot_api1_no_meta_b) == expected[prot_api1_no_meta_b]
    assert check(prot_api1_no_meta_c) == expected[prot_api1_no_meta_c]
    assert check(prot_api1_meta1) == expected[prot_api1_meta1]
    assert check(prot_api1_meta2) == expected[prot_api1_meta2]
    assert check(prot_api2_no_meta) == expected[prot_api2_no_meta]
    assert check(prot_api2_meta1) == expected[prot_api2_meta1]
    assert check(prot_api2_meta2) == expected[prot_api2_meta2]

    # for protocol, expected_version in expected.items():
    #     parsed = ast.parse(protocol, filename='test', mode='exec')
    #     metadata = extract_metadata(parsed)
    #     detected_version = infer_version(metadata, parsed)
    #     assert detected_version == expected_version

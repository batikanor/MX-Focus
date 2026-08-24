"""Tests for serve_focus.

The socket tests are real: a genuine server, a genuine client, over a genuine
loopback connection. No hardware, but no mocking of the transport either --
mocking a socket mostly tests the mock.
"""

import asyncio
import json
import socket

import numpy as np
import pytest

import focus
import serve_focus as sf


def make_score(value=0.7, z=0.4, calibrating=False, artifact=(False,) * 4,
               held=False, t=1.0):
    return focus.FocusScore(
        value=value,
        z=z,
        calibrating=calibrating,
        artifact=np.array(artifact, dtype=bool),
        held=held,
        t=t,
    )


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def wait_for(predicate, timeout=2.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def connect_with_retry(url, timeout=3.0):
    """Connect once the server has bound, without guessing at a sleep."""
    import websockets

    async with asyncio.timeout(timeout):
        while True:
            try:
                return await websockets.connect(url)
            except OSError:
                await asyncio.sleep(0.02)


# --- payload ----------------------------------------------------------------

def test_payload_is_flat_primitives():
    """Unity's JsonUtility cannot handle nesting or dictionaries."""
    payload = sf.to_payload(make_score())

    assert set(payload) == {"t", "value", "z", "calibrating", "artifact", "held"}
    for key, item in payload.items():
        assert isinstance(item, (float, bool)), f"{key} is {type(item).__name__}"

    json.dumps(payload)   # must round-trip


def test_calibration_sends_neutral_never_null():
    """Safety: JsonUtility turns null into 0.0, and 0.0 means maximum wobble.

    A parsing slip must not produce a violently shaking pen the student
    cannot escape, so the wire carries neutral and the flag carries the
    real information.
    """
    payload = sf.to_payload(make_score(value=None, z=None, calibrating=True))

    assert payload["value"] == pytest.approx(0.5)
    assert payload["value"] is not None
    assert payload["calibrating"] is True


def test_artifact_array_collapses_to_one_bool():
    clean = sf.to_payload(make_score(artifact=(False, False, False, False)))
    dirty = sf.to_payload(make_score(artifact=(False, True, False, False)))

    assert clean["artifact"] is False
    assert dirty["artifact"] is True


# --- queue policy -----------------------------------------------------------

def test_offer_drops_oldest_when_full():
    """Stale scores are the correct thing to lose; the pen must not replay."""
    queue = asyncio.Queue(maxsize=3)
    for i in range(6):
        sf.offer(queue, i)

    assert queue.qsize() == 3
    assert [queue.get_nowait() for _ in range(3)] == [3, 4, 5]


def test_offer_never_blocks():
    queue = asyncio.Queue(maxsize=1)
    for i in range(1000):
        sf.offer(queue, i)      # would hang if it blocked
    assert queue.qsize() == 1


# --- the socket -------------------------------------------------------------

@pytest.mark.asyncio
async def test_client_receives_a_score():
    import websockets

    server = sf.FocusServer()
    port = free_port()

    async with websockets.serve(server.handler, "127.0.0.1", port):
        async with websockets.connect(f"ws://127.0.0.1:{port}") as client:
            await wait_for(lambda: server.client_count == 1)
            await server.broadcast(make_score(value=0.73, z=1.2))

            data = json.loads(await asyncio.wait_for(client.recv(), timeout=2))

    assert data["value"] == pytest.approx(0.73)
    assert data["z"] == pytest.approx(1.2)
    assert data["calibrating"] is False


@pytest.mark.asyncio
async def test_broadcast_reaches_every_client():
    import websockets

    server = sf.FocusServer()
    port = free_port()

    async with websockets.serve(server.handler, "127.0.0.1", port):
        async with websockets.connect(f"ws://127.0.0.1:{port}") as a, \
                   websockets.connect(f"ws://127.0.0.1:{port}") as b:
            await wait_for(lambda: server.client_count == 2)
            await server.broadcast(make_score(value=0.42))

            first = json.loads(await asyncio.wait_for(a.recv(), timeout=2))
            second = json.loads(await asyncio.wait_for(b.recv(), timeout=2))

    assert first["value"] == pytest.approx(0.42)
    assert second == first


@pytest.mark.asyncio
async def test_broadcast_with_no_clients_is_harmless():
    server = sf.FocusServer()
    await server.broadcast(make_score())      # must not raise
    assert server.client_count == 0


@pytest.mark.asyncio
async def test_disconnect_deregisters_the_client():
    import websockets

    server = sf.FocusServer()
    port = free_port()

    async with websockets.serve(server.handler, "127.0.0.1", port):
        async with websockets.connect(f"ws://127.0.0.1:{port}"):
            await wait_for(lambda: server.client_count == 1)
        await wait_for(lambda: server.client_count == 0)


# --- the thread bridge ------------------------------------------------------

@pytest.mark.asyncio
async def test_serve_pumps_a_synchronous_source_and_finishes():
    """The whole point of the worker thread: a blocking generator feeds the
    socket without the socket having to become part of the pipeline.

    The source is gated on a client actually being connected. That is not
    test scaffolding hiding a problem -- scores produced before anyone is
    listening are dropped by design, so a test that raced the connection
    would be asserting against that deliberate behaviour.
    """
    import threading
    import time

    import websockets

    port = free_port()
    server = sf.FocusServer()
    listening = threading.Event()
    received = []

    def blocking_source():
        listening.wait(timeout=5.0)
        for i in range(5):
            time.sleep(0.02)        # a genuinely blocking producer
            yield make_score(value=i / 10.0, t=float(i))

    serve_task = asyncio.create_task(
        sf.serve(blocking_source(), host="127.0.0.1", port=port, server=server)
    )

    # serve() binds asynchronously, so retry the connect rather than
    # guessing at a sleep duration
    client = await connect_with_retry(f"ws://127.0.0.1:{port}")
    try:
        await wait_for(lambda: server.client_count == 1)
        listening.set()
        try:
            async with asyncio.timeout(3.0):
                while len(received) < 5:
                    received.append(json.loads(await client.recv()))
        except (TimeoutError, websockets.exceptions.ConnectionClosed):
            pass
    finally:
        await client.close()

    await asyncio.wait_for(serve_task, timeout=3)

    assert [r["t"] for r in received] == [0.0, 1.0, 2.0, 3.0, 4.0]
    assert [r["value"] for r in received] == pytest.approx([0.0, 0.1, 0.2, 0.3, 0.4])

"""Transport: push FocusScore to the VR layer over a local WebSocket.

This replaces the original project's cloud round trip -- EEG -> MQTT -> IoT
Core -> Lambda -> API Gateway -> Unity polling at 100 requests/second -- with
a local socket. Roughly 5 ms instead of several hundred, no bill, and no AWS
account to lose. AWS is not in the realtime path at all.

Design decisions, all settled deliberately rather than defaulted into:

  - Python serves, Unity connects, and the server **pushes**. The pipeline
    produces exactly 4 scores/second, so there is nothing for a client to
    poll *for*. Polling is what made the original burn 100 req/s resampling
    a value that changed 4 times a second.
  - The pipeline runs in a **worker thread** and hands scores to the event
    loop through a queue. bandpower and focus are deliberately plain
    synchronous code that a list can drive in a test -- that property is why
    39 tests run without hardware, and making them async to suit a transport
    detail would destroy it. asyncio stays confined to this file.
  - Port 8766, leaving stream_eeg.py on 8765 untouched. That server and
    receive_eeg.py are the recording path, which is how a real session gets
    captured to validate focus.SQUASH_SIGMA. Replacing them now would remove
    the tool needed to check the thing just built.
  - Scores are **dropped, not buffered**, when nobody is listening or the
    queue backs up. A focus score is only meaningful now; delivering a
    backlog of stale scores on connect would be worse than delivering
    nothing, because the pen would replay history.
  - The payload is flat primitives only. Unity's JsonUtility rejects
    dictionaries and top-level arrays, so a flat object maps to a trivial
    [Serializable] class where nothing can go subtly wrong in parsing.
  - During calibration the wire carries **neutral (0.5), not null**. This is
    a safety choice: JsonUtility turns a missing or null float into 0.0, and
    0.0 in this pipeline means *maximum* wobble. A parsing slip would
    otherwise produce the worst possible behaviour -- a violently shaking pen
    the student cannot escape. Neutral makes that failure benign, and the
    `calibrating` flag still carries the real information.
"""

from __future__ import annotations

import asyncio
import json
import threading

import numpy as np

import focus


HOST = "127.0.0.1"
PORT = 8766

# 2 seconds at 4 Hz. Small on purpose: see the drop-not-buffer note above.
QUEUE_SIZE = 8

NEUTRAL_VALUE = focus.squash(focus.NEUTRAL_Z)

_DONE = object()


def to_payload(score) -> dict:
    """Convert one FocusScore into the flat shape Unity parses.

    `value` is never null. While calibrating it is neutral, so a client that
    ignores the flag degrades to "no effect" rather than "maximum wobble".
    """
    calibrating = bool(score.calibrating)
    value = NEUTRAL_VALUE if score.value is None else float(score.value)
    z = focus.NEUTRAL_Z if score.z is None else float(score.z)

    return {
        "t": float(score.t),
        "value": value,
        "z": z,
        "calibrating": calibrating,
        "artifact": bool(np.any(score.artifact)),
        "held": bool(score.held),
    }


def offer(queue, item):
    """Put an item on the queue, discarding the oldest if it is full.

    Never blocks and never grows. A stalled or absent consumer costs you the
    stale scores, which is the correct thing to lose.
    """
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    queue.put_nowait(item)


class FocusServer:
    """Broadcasts scores to every connected client."""

    def __init__(self):
        self._clients: set = set()

    @property
    def client_count(self) -> int:
        return len(self._clients)

    async def handler(self, websocket):
        """One connected client.

        Every client gets the same broadcast; a debug listener can sit
        alongside Unity without either noticing the other.
        """
        self._clients.add(websocket)
        try:
            await websocket.wait_closed()
        finally:
            self._clients.discard(websocket)

    async def broadcast(self, score):
        """Send one score to whoever is listening. Nobody listening is fine."""
        if not self._clients:
            return

        message = json.dumps(to_payload(score))
        for client in list(self._clients):
            try:
                await client.send(message)
            except Exception:
                # A client that vanished mid-send is not this loop's problem;
                # its handler will clean up. Dropping one client must never
                # take down the others or the pipeline feeding them.
                self._clients.discard(client)

    async def pump(self, queue):
        """Drain the queue into broadcast until the producer signals done."""
        while True:
            item = await queue.get()
            if item is _DONE:
                return
            await self.broadcast(item)


async def serve(source, host=HOST, port=PORT, server=None):
    """Run the server, pumping a synchronous `source` of FocusScore.

    `source` is any iterable -- the live pipeline, a replayed recording, or a
    list in a test. It is consumed on a worker thread so that a blocking read
    cannot stall the socket.
    """
    import websockets

    server = server or FocusServer()
    queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
    loop = asyncio.get_running_loop()

    def worker():
        try:
            for score in source:
                loop.call_soon_threadsafe(offer, queue, score)
        finally:
            loop.call_soon_threadsafe(offer, queue, _DONE)

    thread = threading.Thread(target=worker, daemon=True, name="focus-pipeline")
    thread.start()

    async with websockets.serve(server.handler, host, port):
        await server.pump(queue)


# --- entry point ------------------------------------------------------------

def lsl_source(inlet, n_channels, chunk):
    """Drain an LSL inlet into (samples, timestamps) pairs.

    Lives here rather than in bandpower because bandpower deliberately does
    not import pylsl -- that is what lets it be tested with a plain list.
    """
    while True:
        samples, timestamps = inlet.pull_chunk(timeout=1.0, max_samples=chunk)
        if timestamps:
            yield np.asarray(samples, dtype=float)[:, :n_channels], np.asarray(
                timestamps, dtype=float
            )


def main():
    """Wire LSL -> band power -> focus score -> socket."""
    from pylsl import StreamInlet, resolve_byprop

    import bandpower

    print("Looking for an EEG stream...")
    streams = resolve_byprop("type", "EEG", timeout=5)
    if not streams:
        raise SystemExit("No EEG stream found. Is muselsl streaming?")

    inlet = StreamInlet(streams[0], max_chunklen=12)
    fs = streams[0].nominal_srate()
    print(f"Streaming at {fs} Hz. Serving focus scores on ws://{HOST}:{PORT}")

    scores = focus.score_stream(
        bandpower.extract_stream(
            lsl_source(inlet, len(bandpower.CHANNELS), chunk=12), fs
        )
    )
    asyncio.run(serve(scores))


if __name__ == "__main__":
    main()

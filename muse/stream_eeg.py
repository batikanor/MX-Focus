import asyncio
import json
from websockets import serve
from pylsl import StreamInlet, resolve_byprop
from constants import LSL_SCAN_TIMEOUT, LSL_EEG_CHUNK  # Adjust import path as needed

async def stream_eeg(websocket):  # Ignoring 'path' argument
    print("Looking for an EEG stream...")
    streams = resolve_byprop('type', 'EEG', timeout=LSL_SCAN_TIMEOUT)

    if not streams:
        print("No EEG stream found.")
        await websocket.send(json.dumps({"error": "No EEG stream found."}))
        return

    inlet = StreamInlet(streams[0], max_chunklen=LSL_EEG_CHUNK)
    print("Started acquiring EEG data.")
    
    try:
        while True:
            data, timestamps = inlet.pull_chunk(timeout=1.0, max_samples=1)
            if timestamps and data:
                message = json.dumps({"timestamps": timestamps, "data": data})
                await websocket.send(message)
                await asyncio.sleep(1)

    except Exception as e:
        print(f"Error during streaming: {e}")
    finally:
        print("Connection closed.")

# Start WebSocket server
async def main():
    print("Starting WebSocket server...")
    async with serve(stream_eeg, "localhost", 8765):  # Now works correctly
        print("WebSocket server started at ws://localhost:8765")
        await asyncio.Future()  # Run forever

if __name__ == "__main__":
    asyncio.run(main())

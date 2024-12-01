import asyncio
import websockets
import json
import pandas as pd
from datetime import datetime

async def save_data(data):
    # Define the CSV file name with the current timestamp
    filename = f"eeg_data_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.csv"
    
    # Convert received data into a DataFrame
    df = pd.DataFrame(data['data'], columns=["EEG_" + str(i) for i in range(len(data['data'][0]))])
    df['timestamps'] = data['timestamps']
    
    # Save the DataFrame to a CSV file
    df.to_csv(filename, index=False)
    print(f"Data saved to {filename}")

async def receive_eeg():
    uri = "ws://localhost:8765"  # WebSocket server address
    async with websockets.connect(uri) as websocket:
        try:
            while True:
                message = await websocket.recv()  # Receive message from server
                data = json.loads(message)  # Convert JSON string to Python object

                # Check for error messages
                if "error" in data:
                    print(f"Error: {data['error']}")
                    break

                timestamps = data["timestamps"]
                eeg_data = data["data"]
                
                # Print the EEG data (timestamps and samples)
                print(f"Received data: Timestamps: {timestamps[:5]}... Data: {eeg_data[:5]}...")  # Printing only first 5 samples for brevity
                await save_data(data)

        except websockets.exceptions.ConnectionClosed:
            print("Connection closed by server.")
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(receive_eeg())  # Run the client

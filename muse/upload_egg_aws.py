import json
import time
import ssl
from paho.mqtt import client as mqtt_client
import numpy as np

# AWS IoT Core settings
broker = "as99blacz9jf-ats.iot.us-west-2.amazonaws.com"
port = 8883
client_id = "EEGStreamer"
topic = "eeg/data"

# Paths to certificates
cert_path = "creds/5ad65160b7ff269dee9a19b89deb1020f92f2c1a7d3775f7af8085f23211a8fc-certificate.pem.crt"
key_path = "creds/5ad65160b7ff269dee9a19b89deb1020f92f2c1a7d3775f7af8085f23211a8fc-private.pem.key"
ca_path = "creds/AmazonRootCA1.pem"

# Connect to MQTT broker
def connect_mqtt():
    def on_connect(client, userdata, flags, rc):
        if rc == 0:
            print("Connected to AWS IoT Core")
        else:
            print(f"Failed to connect, return code {rc}")

    client = mqtt_client.Client(mqtt_client.CallbackAPIVersion.VERSION1, client_id)
    client.tls_set(ca_certs=ca_path, certfile=cert_path, keyfile=key_path, cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLSv1_2)
    client.tls_insecure_set(False)
    client.on_connect = on_connect
    client.connect(broker, port)
    return client

# Publish EEG data
def publish(client, eeg_data):
    payload = json.dumps(eeg_data)
    result = client.publish(topic, payload)
    status = result[0]
    if status == 0:
        print(f"Sent: {payload}")
    else:
        print(f"Failed to send message to topic {topic}")

# Simulate EEG data streaming
def stream_eeg():
    client = connect_mqtt()
    client.loop_start()
    try:
        while True:
            random = np.random.rand(1)
            eeg_data = {
                "timestamp": time.time(),
                "data": random.tolist(),  # Replace with actual EEG data
            }
            publish(client, eeg_data)
            time.sleep(0.5)  # Adjust based on your sampling rate
    except KeyboardInterrupt:
        print("Stopping EEG stream...")
    finally:
        client.loop_stop()
        client.disconnect()

if __name__ == "__main__":
    stream_eeg()



# https://rqx4xasush.execute-api.us-west-2.amazonaws.com/production/eeg
# curl -X POST https://rqx4xasush.execute-api.us-west-2.amazonaws.com/production/eeg \
#   -H "Content-Type: application/json" \
#   -d '{"eeg_data": {"timestamp": 1733037104.8290932, "channels": [1.0, 2.0, 3.0, 4.0]}}'

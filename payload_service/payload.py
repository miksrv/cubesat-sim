import time
import paho.mqtt.client as mqtt
from picamera2 import Picamera2
from pathlib import Path

BROKER = "localhost"
PORT = 1883

OBC_COMMAND_TOPIC = "cubesat/obc/commands"
PAYLOAD_PHOTO_TOPIC = "cubesat/payload/photo"

PHOTO_DIR = Path("photos")
PHOTO_DIR.mkdir(exist_ok=True)

photo_counter = 0

def take_photo():
    global photo_counter
    photo_counter += 1
    filename = PHOTO_DIR / f"photo_{photo_counter}.jpg"

    try:
        picam = Picamera2()
        picam.start_preview()
        time.sleep(2)
        picam.capture_file(str(filename))
        picam.stop_preview()
    except Exception as e:
        print(f"Payload: Error taking photo: {e}")
        return None

    print(f"Payload: took photo {filename}")
    return str(filename)

def on_connect(client, userdata, flags, rc):
    print("Payload connected to broker")
    client.subscribe(OBC_COMMAND_TOPIC, qos=1)

def on_message(client, userdata, msg):
    command = msg.payload.decode()
    print(f"Payload received command: {command}")

    if command == "take_photo":
        photo_path = take_photo()
        if photo_path:
            client.publish(PAYLOAD_PHOTO_TOPIC, photo_path, qos=1)

def main():
    client = mqtt.Client(client_id="Payload", callback_api_version=1)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER, PORT, 60)
    client.loop_forever()

if __name__ == "__main__":
    main()
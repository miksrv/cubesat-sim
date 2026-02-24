import time
import paho.mqtt.client as mqtt

BROKER = "localhost"
PORT = 1883

COMMAND_TOPIC = "cubesat/command"          # команды извне (Telegram)
OBC_TO_PAYLOAD_TOPIC = "cubesat/obc/commands"  # команды, разрешенные OBC
OBC_STATE_TOPIC = "cubesat/obc/state"

satellite_state = "NOMINAL"

def on_connect(client, userdata, flags, rc):
    print("OBC connected to broker")
    client.subscribe(COMMAND_TOPIC, qos=1)

def on_message(client, userdata, msg):
    global satellite_state
    command = msg.payload.decode()
    print(f"OBC received command: {command}")

    if satellite_state in ["LOW_POWER", "SAFE"]:
        print(f"OBC: cannot execute command in {satellite_state} mode")
        return

    if command == "take_photo":
        print("OBC: forwarding 'take_photo' to Payload")
        client.publish(OBC_TO_PAYLOAD_TOPIC, command, qos=1)

def main():
    client = mqtt.Client(client_id="OBC", callback_api_version=1)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(BROKER, PORT, 60)
    client.loop_forever()

if __name__ == "__main__":
    main()
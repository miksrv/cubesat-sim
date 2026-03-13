import json
import logging

logger = logging.getLogger(__name__)

class OBCMessageHandlers:
    def __init__(self, obc):
        self.obc = obc

    def handle_eps_status(self, payload):
        try:
            data = json.loads(payload)
            battery = data.get('battery', 100)
            external = data.get('external_power', False)

            if battery < 20:
                self.obc.state_machine.enter_safe_mode()
            elif battery < 40 and self.obc.state_machine.state not in ['LOW_POWER', 'SAFE']:
                self.obc.state_machine.enter_low_power()
            elif external and self.obc.state_machine.state in ['LOW_POWER', 'SAFE']:
                self.obc.state_machine.recover()
        except Exception as e:
            logger.error(f"Error processing EPS status: {e}")

    def handle_command(self, payload):
        try:
            cmd = json.loads(payload)
            command = cmd.get('command')
            logger.info(f"Command received: {command}")

            if command == 'science_start':
                if self.obc.state_machine.state == 'NOMINAL':
                    self.obc.state_machine.start_science()
            elif command == 'science_stop':
                if self.obc.state_machine.state == 'SCIENCE':
                    self.obc.state_machine.end_science()
            elif command == 'safe_mode':
                self.obc.state_machine.enter_safe_mode()
            elif command == 'recover':
                self.obc.state_machine.recover()
            # Add your own commands
        except Exception as e:
            logger.error(f"Error processing command: {e}")

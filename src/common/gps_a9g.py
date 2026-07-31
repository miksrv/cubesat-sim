import serial
import pynmea2
from typing import Dict, Optional

from src.common.config import GPS_PORT, GPS_BAUDRATE


class GPS:
    """NMEA reader for the A9G GPS/BDS module (52Pi IoT Node(A)), exposed on /dev/ttySC1."""

    def __init__(self, port: str = GPS_PORT, baudrate: int = GPS_BAUDRATE, timeout: float = 0.2):
        self.ser = serial.Serial(port, baudrate, timeout=timeout)
        self._last_fix: Dict[str, Optional[float]] = self._empty_fix()

    @staticmethod
    def _empty_fix() -> Dict[str, Optional[float]]:
        return {"lat": None, "lon": None, "alt": None, "speed": None, "fix": False}

    def read_position(self) -> Dict[str, Optional[float]]:
        """Reads whatever NMEA sentences are available within the serial timeout budget.
        Never blocks longer than the configured timeout, even with no fix — returns the
        last known-good values with fix=False rather than raising."""
        while self.ser.in_waiting:
            try:
                line = self.ser.readline().decode("ascii", errors="ignore").strip()
            except serial.SerialException:
                break

            if not line.startswith("$"):
                continue

            try:
                msg = pynmea2.parse(line)
            except pynmea2.ParseError:
                continue

            if isinstance(msg, pynmea2.types.talker.GGA) and msg.gps_qual and msg.gps_qual > 0:
                self._last_fix["lat"] = msg.latitude
                self._last_fix["lon"] = msg.longitude
                self._last_fix["alt"] = msg.altitude
                self._last_fix["fix"] = True
            elif isinstance(msg, pynmea2.types.talker.RMC) and msg.status == "A":
                self._last_fix["lat"] = msg.latitude
                self._last_fix["lon"] = msg.longitude
                self._last_fix["speed"] = msg.spd_over_grnd
                self._last_fix["fix"] = True

        return dict(self._last_fix)

    def close(self):
        self.ser.close()

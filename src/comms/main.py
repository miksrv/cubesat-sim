import logging
from src.common import setup_logging

setup_logging(
    log_level = "INFO",
    log_file  = "comms.log",
    console   = True
)

from src.comms.service import CommsService

if __name__ == "__main__":
    comms = CommsService()
    comms.run()

"""Entry point: ``python -m cubesat.comms``."""

from __future__ import annotations

from cubesat.common.log import setup_logging
from cubesat.comms.service import CommsService


def main() -> None:
    setup_logging("comms")
    CommsService().run()


if __name__ == "__main__":
    main()

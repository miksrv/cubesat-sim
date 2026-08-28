"""Entry point: ``python -m cubesat.payload``."""

from __future__ import annotations

from cubesat.common.log import setup_logging
from cubesat.payload.service import PayloadService


def main() -> None:
    setup_logging("payload")
    PayloadService().run()


if __name__ == "__main__":
    main()

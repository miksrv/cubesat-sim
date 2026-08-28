"""Entry point: ``python -m cubesat.eps``."""

from __future__ import annotations

from cubesat.common.log import setup_logging
from cubesat.eps.service import EpsService


def main() -> None:
    setup_logging("eps")
    EpsService().run()


if __name__ == "__main__":
    main()

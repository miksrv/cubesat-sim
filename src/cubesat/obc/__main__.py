"""Entry point: ``python -m cubesat.obc``."""

from __future__ import annotations

from cubesat.common.log import setup_logging
from cubesat.obc.service import ObcService


def main() -> None:
    setup_logging("obc")
    ObcService().run()


if __name__ == "__main__":
    main()

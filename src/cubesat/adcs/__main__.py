"""Entry point: ``python -m cubesat.adcs``."""

from __future__ import annotations

from cubesat.adcs.service import AdcsService
from cubesat.common.log import setup_logging


def main() -> None:
    setup_logging("adcs")
    AdcsService().run()


if __name__ == "__main__":
    main()

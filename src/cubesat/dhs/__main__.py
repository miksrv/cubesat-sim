"""Entry point: ``python -m cubesat.dhs``."""

from __future__ import annotations

from cubesat.common.log import setup_logging
from cubesat.dhs.service import DhsService


def main() -> None:
    setup_logging("dhs")
    DhsService().run()


if __name__ == "__main__":
    main()

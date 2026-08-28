"""Entry point: ``python -m cubesat.dashboard``."""

from __future__ import annotations

from cubesat.common.log import setup_logging
from cubesat.dashboard.service import DashboardService


def main() -> None:
    setup_logging("dashboard")
    DashboardService().run()


if __name__ == "__main__":
    main()

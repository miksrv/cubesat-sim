"""Entry point: ``python -m cubesat.hostd`` (as root, or with CUBESAT_MOCK_HOST=1)."""

from __future__ import annotations

import sys

from cubesat.common.log import setup_logging
from cubesat.hostd.executor import PrivilegeError
from cubesat.hostd.service import HostdService


def main() -> None:
    setup_logging("hostd")
    try:
        service = HostdService()
    except PrivilegeError as exc:
        # A clear message and a non-zero exit, rather than a service that comes
        # up, fails every systemctl call it makes, and reports profiles as
        # applied. See executor.select_executor().
        sys.exit(f"hostd: {exc}")
    service.run()


if __name__ == "__main__":
    main()

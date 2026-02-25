import logging
from ..common.logging import setup_logging

setup_logging(
    log_level    = "INFO",
    log_file     = "telemetry.log",
    console      = True
)

import sys

from aggregator import TelemetryAggregator

if __name__ == "__main__":
    agg = TelemetryAggregator()
    agg.run()
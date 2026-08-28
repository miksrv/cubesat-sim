"""Mission state to poll interval.

One table instead of a hardcoded ``time.sleep`` in every service, because
``LOW_POWER`` only means something if every subsystem actually slows down when
it is entered. The table lives in ``config/config.yaml``; this module is the
lookup with its fallbacks.

A profile may additionally scale every interval — ``DIAG`` runs everything
faster than nominal to shake out hardware, and it does so with one number in
``profiles.yaml`` rather than a second copy of this table.
"""

from __future__ import annotations

from cubesat.common import config
from cubesat.common.states import MissionState

#: Used when neither the service nor a ``default`` key is in the table.
FALLBACK_INTERVAL_SEC = 30.0


def interval_for(
    service: str,
    state: MissionState | None,
    scale: float = 1.0,
) -> float:
    """Return the poll interval in seconds for ``service`` in ``state``.

    ``scale`` comes from the active profile (``power.cadence_scale``); values
    below 1 poll faster. A resolved interval of 0 means "do not poll at all" —
    used for the radio in ``SAFE`` — and is returned unscaled.
    """
    table = config.CADENCE.get(service, {})
    raw = None
    if state is not None:
        raw = table.get(state.value)
    if raw is None:
        raw = table.get("default")
    if raw is None:
        raw = FALLBACK_INTERVAL_SEC
    interval = float(raw)
    if interval <= 0:
        return 0.0
    return max(0.01, interval * scale)

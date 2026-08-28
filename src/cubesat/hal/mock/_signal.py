"""Shared helpers for the mock drivers.

Mocks produce slowly varying, plausible values rather than constants. Flat lines
hide bugs: a dashboard chart, a timeline and a track are all much easier to get
wrong when every reading is identical, and the mocks are what the dashboard is
developed against.
"""

from __future__ import annotations

import math
import time


def wave(period_sec: float, amplitude: float, offset: float = 0.0, phase: float = 0.0) -> float:
    """A sine wave in wall-clock time. Deterministic for a given moment."""
    return offset + amplitude * math.sin(2 * math.pi * (time.time() / period_sec + phase))


def drift(period_sec: float, low: float, high: float) -> float:
    """A value sweeping between ``low`` and ``high`` with the given period."""
    span = (high - low) / 2
    return wave(period_sec, span, low + span)

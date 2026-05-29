"""
Robust statistics utilities for signal modules — rolling window design.

Design principle:
  hist = rolling window of N observations (caller controls N via DB LIMIT).
  As the market shifts structurally, the window slides and the distribution
  recalibrates naturally. No hard-coded normality assumptions.

Two-layer approach:
  Percentile rank  — distribution-free. Adapts to structural regime shifts.
                     If sugar permanently moves to higher contango, this
                     recalibrates over time without any code change.
  Modified Z-score — Iglewicz & Hoaglin (1993), MAD-based. Detects short-term
                     anomalies within the current regime. Robust to fat tails
                     because MAD is nearly immune to outliers (breakdown point 0.5).

AND gate conviction:
  HIGH   — both percentile AND modified_z confirm extreme (prevents fat-tail
            false signals: a single Black Swan moves mZ but not percentile)
  MEDIUM — only one confirms (possible signal, low confidence)
  LOW    — neither confirms
"""
from typing import Optional

# Thresholds — intentionally slightly looser than COT (90/10) because
# spread distributions have more noise than positioning data.
_EXTREME_PCT_HIGH = 80.0
_EXTREME_PCT_LOW  = 20.0
_EXTREME_MZ       = 2.0    # Iglewicz & Hoaglin threshold (paper uses 3.5 for outliers;
                            # we use 2.0 as "notable" — equivalent to ~Z=1.96 under normality)


def robust_stats(hist: list[float], current: float) -> dict:
    """
    Computes robust stats for a rolling window.

    Parameters
    ----------
    hist    : rolling window of N historical observations
              (DB: ORDER BY date DESC LIMIT N — caller controls window size)
    current : new observation to evaluate against the window

    Returns
    -------
    dict with:
      percentile_rank  : 0–100, rank of current in hist (distribution-free)
      modified_z       : Iglewicz-Hoaglin robust Z-score (MAD-based, fat-tail safe)
      median           : window median
      mad              : Median Absolute Deviation of window
      conviction       : "HIGH" / "MEDIUM" / "LOW" / "INSUFFICIENT_DATA"
      is_extreme_high  : percentile > 80 AND mZ > 2.0  (AND gate)
      is_extreme_low   : percentile < 20 AND mZ < -2.0 (AND gate)
    """
    n = len(hist)
    if n < 10:
        return {
            "percentile_rank": None,
            "modified_z":      None,
            "median":          None,
            "mad":             None,
            "conviction":      "INSUFFICIENT_DATA",
            "is_extreme_high": False,
            "is_extreme_low":  False,
        }

    # ── Percentile rank (distribution-free) ──────────────────────────────────
    percentile_rank = sum(1 for v in hist if v <= current) / n * 100

    # ── Median ────────────────────────────────────────────────────────────────
    sorted_h = sorted(hist)
    mid = n // 2
    if n % 2 == 1:
        median = sorted_h[mid]
    else:
        median = (sorted_h[mid - 1] + sorted_h[mid]) / 2.0

    # ── MAD (Median Absolute Deviation) ──────────────────────────────────────
    deviations = sorted(abs(v - median) for v in hist)
    dev_mid    = len(deviations) // 2
    if len(deviations) % 2 == 1:
        mad = deviations[dev_mid]
    else:
        mad = (deviations[dev_mid - 1] + deviations[dev_mid]) / 2.0

    # ── Modified Z-score (Iglewicz & Hoaglin 1993) ───────────────────────────
    # 0.6745 = Φ⁻¹(0.75): makes mZ consistent with std-Z under normality
    modified_z = (0.6745 * (current - median) / mad) if mad > 0.001 else 0.0

    # ── AND gate — prevents fat-tail false signals ────────────────────────────
    # A single extreme event moves mZ sharply but the percentile rank changes
    # gradually (it takes many new readings for the window to recalibrate).
    # Requiring both layers to confirm filters Black Swan noise.
    is_extreme_high = (percentile_rank > _EXTREME_PCT_HIGH and modified_z >  _EXTREME_MZ)
    is_extreme_low  = (percentile_rank < _EXTREME_PCT_LOW  and modified_z < -_EXTREME_MZ)

    is_high = (percentile_rank > _EXTREME_PCT_HIGH or modified_z >  _EXTREME_MZ)
    is_low  = (percentile_rank < _EXTREME_PCT_LOW  or modified_z < -_EXTREME_MZ)

    if is_extreme_high or is_extreme_low:
        conviction = "HIGH"
    elif is_high or is_low:
        conviction = "MEDIUM"
    else:
        conviction = "LOW"

    return {
        "percentile_rank": round(percentile_rank, 1),
        "modified_z":      round(modified_z, 3),
        "median":          round(median, 6),
        "mad":             round(mad, 6),
        "conviction":      conviction,
        "is_extreme_high": is_extreme_high,
        "is_extreme_low":  is_extreme_low,
    }

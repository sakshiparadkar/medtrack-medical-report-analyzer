"""
MedTrack analytics: severity levels, anomaly detection, next-value prediction.
Pure Python + NumPy, no Flask/DB imports, so it is easy to test and explain.
"""
from datetime import date

import numpy as np

# ------------------------------------------------------------ SEVERITY ----
SEVERITY_LABELS = {
    "normal": "Normal",
    "mild": "Mild",
    "moderate": "Moderate",
    "severe": "Severe",
}
SEVERITY_COLORS = {
    "normal": "#0F766E",
    "mild": "#EAB308",      # yellow
    "moderate": "#EA580C",  # orange
    "severe": "#DC2626",    # red
}

# Deviation = how far OUTSIDE the reference range, as a fraction of range width.
MILD_LIMIT = 0.10       # <= 10% of range width outside  -> yellow
MODERATE_LIMIT = 0.30   # <= 30%                         -> orange, beyond -> red


def normalize(value, ref_min, ref_max):
    """0 = lower limit, 1 = upper limit. None if the range is unusable."""
    if value is None or ref_min is None or ref_max is None or ref_max <= ref_min:
        return None
    return (value - ref_min) / (ref_max - ref_min)


def classify_severity(value, ref_min, ref_max):
    norm = normalize(value, ref_min, ref_max)
    if norm is None:
        return None

    if norm < 0:
        dev, direction = -norm, "low"
    elif norm > 1:
        dev, direction = norm - 1, "high"
    else:
        dev, direction = 0.0, "in range"

    if dev == 0:
        level = "normal"
    elif dev <= MILD_LIMIT:
        level = "mild"
    elif dev <= MODERATE_LIMIT:
        level = "moderate"
    else:
        level = "severe"

    return {
        "level": level,
        "norm": norm,
        "deviation_pct": round(dev * 100, 1),
        "direction": direction,
    }


# ------------------------------------------------------------ ANOMALY ----
MIN_POINTS = 4       # need 3+ other values as baseline (set to 3 for a quick demo)
Z_LIMIT = 2.0        # |z| above this = "outside your normal pattern"
NOISE_FLOOR = 0.05   # std can't be smaller than 5% of the mean (avoids false alarms)


def detect_anomalies(values):
    """
    Leave-one-out z-score: each value is compared with the mean/std of ALL
    OTHER values of the same test.  z = (x - mean) / std
    Returns a list aligned with `values`: {z, expected, is_anomaly}
    """
    n = len(values)
    arr = np.asarray(values, dtype=float)
    out = []
    for i in range(n):
        if n < MIN_POINTS:
            out.append({"z": 0.0, "expected": None, "is_anomaly": False})
            continue
        baseline = np.delete(arr, i)
        mean = float(baseline.mean())
        std = max(float(baseline.std(ddof=1)), NOISE_FLOOR * abs(mean), 1e-9)
        z = (arr[i] - mean) / std
        out.append({
            "z": round(float(z), 2),
            "expected": mean,
            "is_anomaly": bool(abs(z) > Z_LIMIT),
        })
    return out


# ---------------------------------------------------------- PREDICTION ----
def predict_next(dates, values):
    """
    Linear regression (value vs. date) -> expected value at the next report.
    Next date = last date + median gap between your reports.
    """
    if len(values) < 3:
        return None

    x0 = dates[0].toordinal()
    x = np.array([d.toordinal() - x0 for d in dates], dtype=float)
    y = np.asarray(values, dtype=float)
    if np.ptp(x) == 0:
        return None

    slope, intercept = np.polyfit(x, y, 1)

    gaps = np.diff(x)
    gaps = gaps[gaps > 0]
    step = float(np.median(gaps)) if gaps.size else 30.0

    next_x = x[-1] + step
    return {
        "date": date.fromordinal(int(round(x0 + next_x))),
        "value": max(0.0, float(slope * next_x + intercept)),
    }


def analyze_series(rows):
    """rows: [(report_date, value, ref_min, ref_max), ...] sorted by date."""
    dates = [r[0] for r in rows]
    values = [r[1] for r in rows]
    return {
        "dates": dates,
        "values": values,
        "checks": detect_anomalies(values),
        "prediction": predict_next(dates, values),
    }
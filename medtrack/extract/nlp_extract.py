"""
Rule-based structured information extraction for lab reports.

Why rule-based (regex + reference list) instead of a trained ML/NLP model:
Medical lab reports are semi-structured -- each line typically follows the
pattern "<test name> <value> <unit> (<reference range>)". Because the format
is predictable, a trained model is unnecessary: it would need a large
labeled dataset to reach the accuracy a handful of well-designed regex
patterns already achieve, and its behavior would be harder to explain/debug.

Pipeline per line:
  1. Check if a known test name (or alias) from the reference list appears.
  2. Extract the first number after the test name as the value.
  3. Try to extract a unit token right after the value.
  4. Try to extract a "min-max" reference range from the same line.
  5. Flag the result:
       - "needs_review" if no unit could be confidently detected
       - "abnormal"     if the value falls outside the reference range
       - "normal"       otherwise
"""

import json
import re
from pathlib import Path

REFERENCE_PATH = Path(__file__).resolve().parent.parent / "data" / "test_reference.json"

with open(REFERENCE_PATH, "r", encoding="utf-8") as f:
    TEST_REFERENCE = json.load(f)

# Build a flat (alias -> canonical name) lookup, longest alias first so that
# e.g. "sgot/ast" is matched before the shorter "ast" alias inside it.
_ALIAS_MAP = []
for canonical, info in TEST_REFERENCE.items():
    for alias in info["aliases"]:
        _ALIAS_MAP.append((alias.lower(), canonical))
_ALIAS_MAP.sort(key=lambda pair: -len(pair[0]))

_NUMBER_RE = re.compile(r"\d+\.?\d*")
_UNIT_AFTER_NUMBER_RE = re.compile(r"\d+\.?\d*\s*([a-zA-Z%^/]{1,15})")
_RANGE_RE = re.compile(r"(\d+\.?\d*)\s*-\s*(\d+\.?\d*)")


def extract_test_results(text: str):
    """
    Parse raw report text and return a list of dicts:
    {test_name, value, unit, ref_min, ref_max, flag}
    Only the FIRST alias match per line is used, and each canonical test
    name is only recorded once per report (first occurrence wins) to avoid
    duplicate/noisy rows.
    """
    results = []
    seen_in_report = set()

    for raw_line in text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        lower = line.lower()

        for alias, canonical in _ALIAS_MAP:
            if canonical in seen_in_report:
                continue

            pattern = r"\b" + re.escape(alias) + r"\b"
            match = re.search(pattern, lower)
            if not match:
                continue

            rest_of_line = line[match.end():]
            numbers = _NUMBER_RE.findall(rest_of_line)
            if not numbers:
                continue  # test name mentioned but no numeric value found

            value = float(numbers[0])

            unit_match = _UNIT_AFTER_NUMBER_RE.search(rest_of_line)
            unit = unit_match.group(1) if unit_match else None

            range_match = _RANGE_RE.search(rest_of_line)
            if range_match:
                ref_min = float(range_match.group(1))
                ref_max = float(range_match.group(2))
            else:
                ref_min = TEST_REFERENCE[canonical]["ref_min"]
                ref_max = TEST_REFERENCE[canonical]["ref_max"]

            if unit is None:
                flag = "needs_review"
                unit = TEST_REFERENCE[canonical]["unit"]
            elif value < ref_min or value > ref_max:
                flag = "abnormal"
            else:
                flag = "normal"

            results.append({
                "test_name": canonical,
                "value": value,
                "unit": unit,
                "ref_min": ref_min,
                "ref_max": ref_max,
                "flag": flag,
            })
            seen_in_report.add(canonical)
            break  # move to next line once this line is matched

    return results

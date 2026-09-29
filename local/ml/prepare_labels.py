"""Restore comparison scores only when the historical feature vector matches.

The original database snapshot is retained. Unmatched or ambiguous rows remain
missing; no current rule is rerun on historical inputs to invent a baseline.
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

from quant.analyze.ml_scorer import FEATURE_NAMES
from quant.control.judgment import selection_attributes
from quant.control.selections import WATCH_JOIN_PRODUCER, load


def _same_features(left: dict, right: dict) -> bool:
    for name in FEATURE_NAMES:
        a, b = left.get(name), right.get(name)
        if a is None or b is None:
            if a is not b:
                return False
        elif not math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9):
            return False
    return True


def restore_baselines(labels: list[dict], selections: list[dict]) -> list[dict]:
    candidates = defaultdict(list)
    for row in selections:
        if row.get("producer") not in (None, WATCH_JOIN_PRODUCER):
            continue
        attrs = selection_attributes(row)
        score = attrs.get("baseline_score100")
        if score is not None and math.isfinite(float(score)) and 0 <= float(score) <= 100:
            candidates[(row.get("market"), row.get("date"), row.get("symbol"))].append(attrs)
    output = []
    for source in labels:
        row = dict(source)
        if row.get("baseline_score100") is None:
            matches = candidates[(row.get("market"), row.get("session_date"), row.get("symbol"))]
            scores = {float(a["baseline_score100"]) for a in matches if _same_features(row, a)}
            if len(scores) == 1:
                row["baseline_score100"] = scores.pop()
                row["baseline_source"] = "selection_exact_features"
        output.append(row)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--labeled-json", required=True, type=Path)
    parser.add_argument("--selections", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    rows = restore_baselines(json.loads(args.labeled_json.read_text()), load(args.selections))
    args.out.write_text(json.dumps(rows, ensure_ascii=False))
    for market in ("KR", "US"):
        matched = [r for r in rows if r.get("market") == market and r.get("baseline_score100") is not None]
        print(f"[ml] {market} historical baseline: {len(matched)} rows / "
              f"{len({r['session_date'] for r in matched})} dates (matched subset only)")


if __name__ == "__main__":
    main()

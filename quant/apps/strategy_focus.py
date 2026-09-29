"""Archive current catalyst inputs for prospective research."""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

from quant.adapters.focus_capture import capture_focus_inputs
from quant.core.report_clock import KST


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    args = parser.parse_args()
    print(capture_focus_inputs(args.root, now=datetime.now(KST)))


if __name__ == '__main__':
    main()

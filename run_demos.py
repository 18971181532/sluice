#!/usr/bin/env python3
"""Run every demonstration, one after another."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sluice import demos  # noqa: E402

if __name__ == "__main__":
    for demo in demos.build_all():
        print(demo.title)
        print("=" * len(demo.title))
        print()
        print(demo.body)
        print()
        print()
    sys.exit(0)

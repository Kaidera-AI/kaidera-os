#!/usr/bin/env python3
"""Print Beat's effective Cortex runtime profile."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from cortex.runtime_profile import load_runtime_profile, shell_exports


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shell", action="store_true", help="print shell export statements")
    parser.add_argument("--json", action="store_true", help="print JSON profile")
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))  # fitness:allow-literal false-match: root
    args = parser.parse_args()

    profile = load_runtime_profile(Path(args.root))  # fitness:allow-literal false-match: root
    if args.shell:
        print(shell_exports(profile))
    else:
        print(json.dumps(profile, indent=2 if args.json else None, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

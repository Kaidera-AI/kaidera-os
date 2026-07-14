#!/usr/bin/env python3
"""Bake the public edition into a staged Kaidera OS release payload."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


PUBLIC_ASSIGNMENT = '_BAKED_EDITION: str | None = "public"'
SOURCE_ASSIGNMENT = re.compile(
    r"^_BAKED_EDITION\s*:\s*str\s*\|\s*None\s*=\s*None\s*$",
    re.MULTILINE,
)


def bake(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    if PUBLIC_ASSIGNMENT in source:
        compile(source, str(path), "exec")
        return
    baked, replacements = SOURCE_ASSIGNMENT.subn(PUBLIC_ASSIGNMENT, source)
    if replacements != 1:
        raise RuntimeError(
            f"expected one unbaked _BAKED_EDITION assignment in {path}, found {replacements}"
        )
    compile(baked, str(path), "exec")
    path.write_text(baked, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("edition_module", type=Path)
    args = parser.parse_args()
    bake(args.edition_module)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Keep retired brand names confined to documented compatibility surfaces."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
RETIRED_BRAND = re.compile(r"(?i)" + "engen" + r"(?:[-_ ]?os|[-_ ]?ai)")
RETIRED_OS_KEY = "engen" + "os"
HISTORICAL_MIGRATIONS = {
    ".agents/data/appdb/2026-07-11-kaidera-brand-settings.sql",
    ".agents/data/migrations/2026-05-08-phase-c-cortex-app-role.sql",
    ".agents/data/migrations/2026-05-08-phase-c-rls.sql",
    ".agents/data/migrations/2026-05-09-phase-c-rls-audit-extension.sql",
    ".agents/data/migrations/2026-06-15-identity-v2-2-function-fix.sql",
    f".agents/data/migrations/2026-06-24-rename-localdev-to-{RETIRED_OS_KEY}-canonical-text.sql",
    ".agents/data/migrations/2026-06-24-rename-localdev-case-variants.sql",
    ".agents/data/migrations/2026-07-10-kaidera-brand-cutover.sql",
    ".agents/data/migrations/2026-07-11-kaidera-brand-runtime-json.sql",
    ".agents/data/migrations/2026-07-11-kaidera-brand-hyphenated-variants.sql",
}


def source_files() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", ".agents"],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        check=True,
    )
    return [ROOT / line for line in result.stdout.splitlines() if line]


def allowed_occurrence(relative: str, line: str, text: str) -> bool:
    if relative == ".agents/BRAND_COMPATIBILITY.md":
        return True
    if relative in HISTORICAL_MIGRATIONS:
        return True
    return False


def test_retired_brand_names_are_confined_to_compatibility_ledger():
    offenders: list[str] = []
    for path in source_files():
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        relative = str(path.relative_to(ROOT))
        for line_number, line in enumerate(text.splitlines(), 1):
            if RETIRED_BRAND.search(line) and not allowed_occurrence(relative, line, text):
                offenders.append(f"{relative}:{line_number}: {line.strip()}")

    assert offenders == [], "\n".join(offenders)

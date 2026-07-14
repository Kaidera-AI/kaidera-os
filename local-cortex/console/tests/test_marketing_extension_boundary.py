"""Mailbox ingress belongs to the Marketing OS extension, never Kaidera OS core."""

from pathlib import Path


CONSOLE = Path(__file__).resolve().parents[1]
FORBIDDEN = (
    "mailbox-feeders",
    "mailbox_feeders",
    "MAILBOX_ADAPTERS",
    "normalize_mailbox_adapter_event",
    "_MAILBOX_INGRESS_RE",
)


def test_core_runtime_has_no_mailbox_ingress_contract() -> None:
    roots = [CONSOLE / "app", CONSOLE / "spa" / "src", CONSOLE / "data" / "appdb"]
    offenders: list[str] = []
    for root in roots:
        for path in root.rglob("*"):
            if not path.is_file() or path.name.endswith((".pyc", ".map")):
                continue
            if ".test." in path.name or path.name.startswith("test_"):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if any(marker in text for marker in FORBIDDEN):
                offenders.append(str(path.relative_to(CONSOLE)))

    assert offenders == [], (
        "mailbox ingress must move to the Marketing OS extension; core may retain "
        f"only generic automation and outbound identity email: {offenders}"
    )

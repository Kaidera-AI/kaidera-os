from pathlib import Path


def test_dispatch_template_listens_for_handoff_created_events():
    template = (
        Path(__file__).resolve().parents[1]
        / "app"
        / "templates"
        / "_dispatch.html"
    ).read_text(encoding="utf-8")

    assert '"handoff_created"' in template

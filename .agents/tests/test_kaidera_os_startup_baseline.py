import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
VALIDATOR = ROOT / "redistributable" / "scripts" / "validate-cortex-project-config.py"
EXAMPLES = [
    ROOT / "redistributable" / "examples" / "blank.project.json",
    ROOT / "redistributable" / "examples" / "customer-six-role.project.json",
]


def test_project_examples_validate_and_stay_generic():
    for example in EXAMPLES:
        subprocess.run(["python3", str(VALIDATOR), str(example)], check=True, cwd=ROOT)
        text = example.read_text(encoding="utf-8")
        data = json.loads(text)

        assert data["project"]["root"] == "${CORTEX_PROJECT_ROOT}"
        assert "kaidera-os" not in text
        agent_names = {str(agent.get("name", "")).lower() for agent in data["agents"]}
        assert agent_names.isdisjoint({"kai", "ren"})
        assert "project_hex" not in text


def test_blank_project_uses_first_project_model():
    data = json.loads(EXAMPLES[0].read_text(encoding="utf-8"))

    assert data["preset"] == "first-project-minimal"
    assert data["project"]["key"] == "new-project"
    assert {agent["name"] for agent in data["agents"]} == {"lead"}
    assert data["beat"]["orchestrator_agent"] == "lead"
    assert {item["type"] for item in data["model_requirements"]} == {
        "llm",
        "embedding",
        "reranking",
    }


def test_source_tree_does_not_ship_generated_workspace_state():
    generated_paths = [
        ".agents/config/workspace.json",
        ".agents/config/runtime.yaml",
        ".agents/config/beat.env",
        ".agents/rules/kaidera-os.md",
        ".agents/skills/manifest.json",
    ]

    for rel in generated_paths:
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", rel],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        assert result.returncode != 0, f"{rel} is generated deployment state"


def test_operator_docs_describe_clean_first_run_model():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    how_it_works = (ROOT / "docs" / "HOW_IT_WORKS.md").read_text(encoding="utf-8")

    assert "starts with no customer project" in readme
    assert "contains no baked project or worker team" in how_it_works

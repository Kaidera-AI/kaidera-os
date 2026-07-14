from __future__ import annotations

import json

import pytest

from app.settings_module import api as settings_api
from app.settings_module import service
from tests.test_settings_module import FakeOpStore


SECRET = "mfld-secret-never-echo"


def test_manifold_config_shape_masks_secret() -> None:
    result = service.build_providers_config(
        [
            {
                "name": "kaidera-manifold",
                "label": "Kaidera AI Manifold",
                "key_is_set": True,
                "testable": True,
                "key_field": "kaidera_manifold_api_key",
                "base_url": "https://api.kaidera.ai/v1",
                "api_key": SECRET,
            }
        ]
    )

    assert len(result["providers"]) == 1
    row = result["providers"][0]
    assert row["provider_ref"] == "kaidera_manifold_api_key"
    assert row["is_custom"] is False
    assert SECRET not in json.dumps(result)


class FakeProviderConfigSource:
    def builtin_provider_config(self, values):
        return [
            {
                "name": "kaidera-manifold",
                "label": "Kaidera AI Manifold",
                "key_is_set": bool(values.get("kaidera_manifold_api_key")),
                "testable": True,
                "key_field": "kaidera_manifold_api_key",
                "project_id": str(values.get("kaidera_manifold_project_id") or ""),
            }
        ]


@pytest.mark.asyncio
async def test_provider_config_endpoint_is_open_source_manifold_only() -> None:
    store = FakeOpStore(
        app_settings={
            "kaidera_manifold_api_key": SECRET,
            "kaidera_manifold_project_id": "project-1",
        }
    )

    result = await settings_api.providers_config_endpoint(
        "kaidera-os",
        store=store,
        cfg_source=FakeProviderConfigSource(),
    )

    assert "edition" not in result
    assert "byok" not in result
    assert [row["name"] for row in result["providers"]] == ["kaidera-manifold"]
    assert result["providers"][0]["project_id"] == "project-1"
    assert SECRET not in json.dumps(result)

from app import platform_config


def _clear_endpoint_env(monkeypatch):
    for name in (
        platform_config.PLATFORM_URL_ENV,
        platform_config.PORTAL_URL_ENV,
        platform_config.MANIFOLD_BASE_URL_ENV,
    ):
        monkeypatch.delenv(name, raising=False)


def test_endpoints_are_unset_without_explicit_configuration(monkeypatch):
    _clear_endpoint_env(monkeypatch)

    assert platform_config.platform_url() == ""
    assert platform_config.portal_url() == ""
    assert platform_config.manifold_base_url() == ""


def test_platform_origin_supplies_portal_and_manifold_defaults(monkeypatch):
    _clear_endpoint_env(monkeypatch)
    monkeypatch.setenv(platform_config.PLATFORM_URL_ENV, "https://platform.example/")

    assert platform_config.platform_url() == "https://platform.example"
    assert platform_config.portal_url() == "https://platform.example"
    assert platform_config.manifold_base_url() == "https://platform.example/v1"


def test_specific_endpoint_configuration_takes_precedence(monkeypatch):
    _clear_endpoint_env(monkeypatch)
    monkeypatch.setenv(platform_config.PLATFORM_URL_ENV, "https://platform.example")
    monkeypatch.setenv(platform_config.PORTAL_URL_ENV, "https://portal.example/")
    monkeypatch.setenv(platform_config.MANIFOLD_BASE_URL_ENV, "https://inference.example/v1/")

    assert platform_config.portal_url() == "https://portal.example"
    assert platform_config.manifold_base_url() == "https://inference.example/v1"

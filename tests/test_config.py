import pytest

from invoiceguard.config import ConfigError, Settings


def test_defaults():
    s = Settings.from_env({})
    assert s.model == "claude-sonnet-5-5" and s.max_upload_bytes == 10 * 1024 * 1024
    assert s.rate_limit_per_minute == 60 and s.anthropic_api_key is None and s.admin_token is None
    assert s.reader == "claude"


def test_values_are_read_and_trimmed():
    s = Settings.from_env({"ANTHROPIC_API_KEY": " k ", "INVOICEGUARD_MAX_UPLOAD_MB": "3",
                           "INVOICEGUARD_PUBLIC_URL": "https://ig.example.com/", "INVOICEGUARD_ADMIN_TOKEN": "x" * 16})
    assert s.anthropic_api_key == "k" and s.max_upload_bytes == 3 * 1024 * 1024
    assert s.public_base_url == "https://ig.example.com"


@pytest.mark.parametrize("env", [
    {"INVOICEGUARD_MAX_UPLOAD_MB": "ten"}, {"INVOICEGUARD_MAX_UPLOAD_MB": "0"},
    {"INVOICEGUARD_ADMIN_TOKEN": "short"}, {"ANTHROPIC_BASE_URL": "http://evil.example.com"},
    {"INVOICEGUARD_READER": "magic"},
])
def test_bad_values_are_rejected(env):
    with pytest.raises(ConfigError):
        Settings.from_env(env)


def test_local_http_base_url_is_allowed_for_testing():
    assert Settings.from_env({"ANTHROPIC_BASE_URL": "http://localhost:9999"}).anthropic_base_url == "http://localhost:9999"


def test_reader_is_claude_or_local():
    assert Settings.from_env({"INVOICEGUARD_READER": " local "}).reader == "local"
    assert Settings.from_env({"INVOICEGUARD_READER": "CLAUDE"}).reader == "claude"


def test_new_operational_settings():
    s = Settings.from_env({"INVOICEGUARD_TRUST_PROXY": "1", "INVOICEGUARD_MAX_CONCURRENT_READS": "2", "INVOICEGUARD_AUTH_FAILURES_PER_MIN": "5"})
    assert s.trust_proxy is True and s.max_concurrent_reads == 2 and s.auth_failures_per_minute == 5
    d = Settings.from_env({})
    assert d.trust_proxy is False and d.max_concurrent_reads == 4 and d.auth_failures_per_minute == 20
    assert Settings.from_env({"INVOICEGUARD_TRUST_PROXY": "no"}).trust_proxy is False
    with pytest.raises(ConfigError):
        Settings.from_env({"INVOICEGUARD_MAX_CONCURRENT_READS": "0"})

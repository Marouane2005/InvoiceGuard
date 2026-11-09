"""Guards against shipping a release whose pieces disagree with each other."""
import pathlib
import re

from invoiceguard import __version__

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_version_is_identical_everywhere():
    pyproject = re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    assert pyproject == __version__
    assert f"## {__version__}" in (ROOT / "CHANGELOG.md").read_text()


def test_env_example_lists_every_setting_the_code_reads():
    code = (ROOT / "invoiceguard" / "config.py").read_text()
    used = set(re.findall(r'"((?:INVOICEGUARD|ANTHROPIC)_[A-Z_]+)"', code))
    documented = set(re.findall(r"^#?\s*((?:INVOICEGUARD|ANTHROPIC)_[A-Z_]+)=", (ROOT / ".env.example").read_text(), re.M))
    assert used <= documented, used - documented


def test_every_cli_command_is_in_the_readme_or_runbook():
    from invoiceguard.cli import build_parser
    commands = set(build_parser()._subparsers._group_actions[0].choices)
    docs = (ROOT / "README.md").read_text() + (ROOT / "docs" / "RUNBOOK.md").read_text()
    assert [c for c in sorted(commands) if f"invoiceguard {c}" not in docs and c != "validate"] == []


def test_deploy_files_exist_and_agree():
    compose = (ROOT / "docker-compose.prod.yml").read_text()
    assert "INVOICEGUARD_TRUST_PROXY" in compose and "expose:" in compose and "ports:" in compose
    assert compose.count("ports:") == 1   # only the proxy publishes ports; the app is not reachable directly
    assert "condition: service_healthy" in compose
    assert "BACKUP_KEEP:-14" in compose and "invoiceguard backup --dest /backups" in compose
    assert "DOMAIN:?set DOMAIN in .env" in compose
    assert "reverse_proxy invoiceguard:8000" in (ROOT / "deploy" / "Caddyfile").read_text()
    assert "/readyz" in (ROOT / "Dockerfile").read_text()


def test_runtime_and_dev_dependencies_are_pinned_exactly():
    pyproject = (ROOT / "pyproject.toml").read_text()
    names = set(re.findall(r'^\s*"([A-Za-z0-9_.-]+)>=', pyproject, re.M))
    pins = {m.group(1).lower().replace("_", "-") for m in re.finditer(r"^([A-Za-z0-9_.-]+)==", (ROOT / "constraints.txt").read_text(), re.M)}
    assert {n.lower().replace("_", "-") for n in names} <= pins, names - pins
    assert "-c constraints.txt" in (ROOT / "Dockerfile").read_text()


def test_admin_surface_is_closed_by_default_in_the_production_setup():
    caddy = (ROOT / "deploy" / "Caddyfile").read_text()
    assert "path /admin/*" in caddy and "remote_ip" in caddy and "{$ADMIN_ALLOW:127.0.0.1}" in caddy
    assert caddy.index("respond @admin_denied") < caddy.index("reverse_proxy")     # the block comes before traffic is forwarded
    assert "ADMIN_ALLOW" in (ROOT / "docker-compose.prod.yml").read_text() and "ADMIN_ALLOW=" in (ROOT / ".env.example").read_text()


def test_every_variable_the_compose_file_needs_is_in_env_example():
    compose = (ROOT / "docker-compose.prod.yml").read_text()
    needed = set(re.findall(r"\$\{([A-Z_]+)", compose))
    documented = set(re.findall(r"^#?\s*([A-Z_]+)=", (ROOT / ".env.example").read_text(), re.M))
    assert needed <= documented, needed - documented


def test_ci_runs_every_kind_of_check_we_rely_on():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    for needle in ("export_openapi.py --check", "pytest", "dashboard.test.js", "release_smoke.py", "pip-audit", "docker build", "actionlint", "hadolint", "proxy_check.py"):
        assert needle in ci, needle
    assert "package-ecosystem: pip" in (ROOT / ".github" / "dependabot.yml").read_text()

import pathlib
import re
import subprocess
import sys

import pytest
import yaml

from check_release_tag import problem
from release_notes import section
from invoiceguard import __version__

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
CHANGELOG = (ROOT / "CHANGELOG.md").read_text()


def load(path):
    data = yaml.safe_load(path.read_text())
    data["triggers"] = data.get("on", data.get(True))   # PyYAML reads the bare key `on` as the boolean True
    return data


def test_there_are_workflows_and_all_are_valid_yaml_with_jobs():
    assert {p.name for p in WORKFLOWS} == {"ci.yml", "release.yml"}
    assert set(load(ROOT / ".github" / "workflows" / "ci.yml")["jobs"]) == {"test", "lint", "proxy", "audit", "docker"}
    for path in WORKFLOWS:
        data = load(path)
        assert data["triggers"] and data["jobs"], path.name
        for name, job in data["jobs"].items():
            assert job.get("runs-on") and job.get("steps"), (path.name, name)


def test_least_privilege_everywhere():
    for path in WORKFLOWS:
        assert load(path)["permissions"] == {"contents": "read"}, f"{path.name} must start read-only"
    release = load(ROOT / ".github" / "workflows" / "release.yml")
    assert release["jobs"]["verify"].get("permissions") is None             # verification only reads
    assert release["jobs"]["publish"]["permissions"] == {"contents": "write", "packages": "write"}
    assert release["jobs"]["publish"]["needs"] == "verify"                   # nothing is published unless verification passed


def test_the_release_runs_only_for_version_tags_and_checks_them_first():
    release = load(ROOT / ".github" / "workflows" / "release.yml")
    assert release["triggers"] == {"push": {"tags": ["v*.*.*"]}}
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "check_release_tag.py" in text and text.index("check_release_tag.py") < text.index("pytest")


def test_only_the_temporary_github_token_is_ever_used_as_a_secret():
    for path in WORKFLOWS:
        assert set(re.findall(r"secrets\.([A-Za-z_]+)", path.read_text())) <= {"GITHUB_TOKEN"}, path.name


def test_dangerous_triggers_and_unpinned_actions_are_absent():
    for path in WORKFLOWS:
        text = path.read_text()
        assert "pull_request_target" not in text and "workflow_run" not in text
        for action in re.findall(r"uses:\s*([^\s]+)", text):
            assert re.fullmatch(r"[\w.-]+/[\w./-]+@v\d+", action), f"{path.name}: {action} must be pinned to a major version"


def test_the_image_is_pushed_to_the_private_registry_under_a_lowercase_name():
    text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
    assert "ghcr.io" in text and "GITHUB_REPOSITORY,," in text and "${{ github.ref_name }}" in text


def test_templates_and_owners_exist_and_templates_warn_about_customer_data():
    for name in ("bug_report.yml", "feature_request.yml", "config.yml"):
        assert yaml.safe_load((ROOT / ".github" / "ISSUE_TEMPLATE" / name).read_text())
    bug = (ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml").read_text()
    assert "Never paste customer invoices" in bug and "required: true" in bug
    assert yaml.safe_load((ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml").read_text())["blank_issues_enabled"] is False
    assert "No customer data" in (ROOT / ".github" / "pull_request_template.md").read_text()
    assert (ROOT / ".github" / "CODEOWNERS").read_text().strip() == "* @Marouane2005"


def test_setup_guide_covers_the_steps_only_the_owner_can_do():
    guide = (ROOT / "docs" / "GITHUB_SETUP.md").read_text()
    for needle in ("Private", "secret scanning", "push protection", "Dependabot", "git tag v", "Do not add your `ANTHROPIC_API_KEY`", "docker login ghcr.io"):
        assert needle in guide, needle


# ---- release scripts -------------------------------------------------------------------------------
def test_release_notes_come_from_the_matching_changelog_section():
    notes = section(CHANGELOG, __version__)
    assert notes and "##" not in notes
    older = section(CHANGELOG, "1.0.0")
    assert older and older != notes
    assert section(CHANGELOG, "9.9.9") is None and section(CHANGELOG, "1.0") is None


@pytest.mark.parametrize("tag,fragment", [("1.2.3", "must look like"), ("v1.2", "must look like"), ("v1.2.3-rc1", "must look like"),
                                          ("v0.0.1", "does not match the code version")])
def test_bad_tags_are_refused(tag, fragment):
    assert fragment in problem(tag, __version__, CHANGELOG)


def test_a_good_tag_passes_but_only_with_release_notes():
    assert problem(f"v{__version__}", __version__, CHANGELOG) is None
    assert "no section" in problem("v7.7.7", "7.7.7", CHANGELOG)


def test_the_scripts_work_from_the_command_line():
    run = lambda *a: subprocess.run([sys.executable, *a], capture_output=True, text=True, cwd=ROOT)
    assert run("scripts/check_release_tag.py", f"v{__version__}").returncode == 0
    bad = run("scripts/check_release_tag.py", "v0.0.1")
    assert bad.returncode == 1 and "does not match" in bad.stderr
    assert run("scripts/release_notes.py", __version__).stdout.strip() == section(CHANGELOG, __version__)
    assert run("scripts/release_notes.py", "9.9.9").returncode == 1
    assert run("scripts/release_notes.py").returncode == 2

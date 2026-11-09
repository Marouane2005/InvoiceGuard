import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]

SECRET_PATTERN = re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}|ig_[A-Za-z0-9_-]{35,}|hooks\.slack\.com/services/T[A-Z0-9]{6,}/B[A-Z0-9]{6,}/[A-Za-z0-9]{12,}")
SECRET_SKIP = {".git", "__pycache__", ".pytest_cache", "invoiceguard.egg-info", "node_modules",
               ".venv", "venv", ".ruff_cache", ".mypy_cache", "backups", "data"}
SECRET_SUFFIXES = {".py", ".md", ".js", ".json", ".yml", ".yaml", ".toml", ".html", ".css", ".txt", ".example", ""}


def text_files(root: pathlib.Path):
    for path in root.rglob("*"):
        if path.is_file() and not (SECRET_SKIP & set(path.parts)) and path.suffix in SECRET_SUFFIXES:
            yield path


def test_secret_scan_ignores_local_environments(tmp_path):
    planted = tmp_path / ".venv" / "lib" / "bad.py"
    planted.parent.mkdir(parents=True)
    planted.write_text("sk-ant-" + "a" * 24)
    kept = tmp_path / "ok.py"
    kept.write_text("print('hi')\n")
    assert list(text_files(tmp_path)) == [kept]


def test_no_secrets_in_the_repo():
    for path in text_files(ROOT):
        assert not SECRET_PATTERN.search(path.read_text(errors="ignore")), path

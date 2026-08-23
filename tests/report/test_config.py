from pathlib import Path

from quant.adapters.env import DEFAULT_ENV, load_env


def test_default_env_is_repo_root_not_quant_package():
    repo_root = Path(__file__).resolve().parents[2]
    assert DEFAULT_ENV == repo_root / ".env.local"


def test_load_env_parses_keys_and_ignores_comments(tmp_path: Path):
    p = tmp_path / ".env.local"
    p.write_text("# 주석\nFRED_API_KEY=abc123\n\nEIA_API_KEY=\n")
    env = load_env(p)
    assert env["FRED_API_KEY"] == "abc123"
    assert env["EIA_API_KEY"] == ""


def test_load_env_missing_file_returns_empty(tmp_path: Path):
    assert load_env(tmp_path / "nope") == {}


def test_load_env_keeps_equals_inside_value(tmp_path: Path):
    p = tmp_path / ".env.local"
    p.write_text("TOKEN=a=b=c\n")
    assert load_env(p)["TOKEN"] == "a=b=c"

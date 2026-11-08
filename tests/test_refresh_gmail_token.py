import re

from refresh_gmail_token import replace_env_value, update_file


def test_replaces_dotenv_value_and_keeps_other_lines():
    text = "A=1\nGMAIL_REFRESH_TOKEN=old\nB=2\n"
    assert replace_env_value(text, "GMAIL_REFRESH_TOKEN", "new", "=", False) == (
        "A=1\nGMAIL_REFRESH_TOKEN=new\nB=2\n"
    )


def test_replaces_quoted_yaml_value():
    text = 'A: "1"\nGMAIL_REFRESH_TOKEN: "old"\n'
    assert replace_env_value(text, "GMAIL_REFRESH_TOKEN", "n/e+w", ": ", True) == (
        'A: "1"\nGMAIL_REFRESH_TOKEN: "n/e+w"\n'
    )


def test_does_not_match_key_with_same_prefix():
    text = "GMAIL_REFRESH_TOKEN_EXTRA=x\n"
    result = replace_env_value(text, "GMAIL_REFRESH_TOKEN", "new", "=", False)
    assert result == "GMAIL_REFRESH_TOKEN_EXTRA=x\nGMAIL_REFRESH_TOKEN=new\n"


def test_appends_missing_key():
    assert replace_env_value("A=1", "GMAIL_REFRESH_TOKEN", "t", "=", False) == (
        "A=1\nGMAIL_REFRESH_TOKEN=t\n"
    )


def test_update_file_backs_up_into_dated_file_and_skips_missing(tmp_path):
    env = tmp_path / ".env"
    env.write_text("GMAIL_REFRESH_TOKEN=old\n")
    backup_dir = tmp_path / ".bak_logs"
    backup = update_file(env, "new", "=", False, backup_dir)
    assert env.read_text() == "GMAIL_REFRESH_TOKEN=new\n"
    assert backup.parent == backup_dir
    assert re.fullmatch(r"\.env\.\d{4}-\d{2}-\d{2}_\d{6}\.bak", backup.name)
    assert backup.read_text() == "GMAIL_REFRESH_TOKEN=old\n"
    assert update_file(tmp_path / "missing.yaml", "new", ": ", True, backup_dir) is None

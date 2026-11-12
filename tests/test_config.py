from __future__ import annotations

from pathlib import Path

import pytest

from tg_sync.config import Config, ConfigError


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    for name in (
        "TG_BOT_TOKEN",
        "TG_API_ID",
        "TG_API_HASH",
        "TG_SESSION",
        "TG_DOWNLOAD_DIR",
        "TG_TASK_DB",
        "TG_MAX_FILE_SIZE_MB",
        "TG_PROXY",
        "TG_TARGET_CHAT",
        "TG_ALLOWED_USERS",
        "TG_YTDLP_COOKIES",
        "TG_YTDLP_COOKIES_FROM_BROWSER",
        "TG_STATUS_MESSAGES",
        "TG_KEEP_FILES",
        "TG_ALBUM_LIMIT",
        "TG_QUEUE_SIZE",
        "TG_LOG_LEVEL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match="TG_BOT_TOKEN"):
        Config.from_env()


def test_credentials_optional_for_download() -> None:
    config = Config.from_env(require_credentials=False)
    assert config.bot_token == ""
    assert config.api_id == 0


def test_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TG_API_ID", "123")
    monkeypatch.setenv("TG_API_HASH", "abc")
    monkeypatch.setenv("TG_MAX_FILE_SIZE_MB", "100")
    monkeypatch.setenv("TG_STATUS_MESSAGES", "false")
    monkeypatch.setenv("TG_ALBUM_LIMIT", "5")

    config = Config.from_env()

    assert config.bot_token == "123:abc"
    assert config.api_id == 123
    assert config.api_hash == "abc"
    assert config.max_file_size == 100 * 1024 * 1024
    assert config.status_messages is False
    assert config.album_limit == 5


def test_invalid_bot_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_BOT_TOKEN", "not-a-token")
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "x")
    with pytest.raises(ConfigError, match="TG_BOT_TOKEN"):
        Config.from_env()


def test_invalid_int(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_API_ID", "not-a-number")
    with pytest.raises(ConfigError):
        Config.from_env()


def test_allowed_users_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_BOT_TOKEN", "1:x")
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "x")
    monkeypatch.setenv("TG_ALLOWED_USERS", "111, 222 333")
    assert Config.from_env().allowed_users == frozenset({111, 222, 333})


def test_allowed_users_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_BOT_TOKEN", "1:x")
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "x")
    monkeypatch.setenv("TG_ALLOWED_USERS", "111,abc")
    with pytest.raises(ConfigError, match="TG_ALLOWED_USERS"):
        Config.from_env()


def test_task_db_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_BOT_TOKEN", "1:x")
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "x")
    assert Config.from_env().task_db == Path("state/tasks.db")

    monkeypatch.setenv("TG_TASK_DB", "/tmp/custom.db")
    assert Config.from_env().task_db == Path("/tmp/custom.db")


def test_workers_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_BOT_TOKEN", "1:x")
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "x")
    monkeypatch.setenv("TG_WORKERS", "99")
    assert Config.from_env().workers == 8
    monkeypatch.setenv("TG_WORKERS", "0")
    assert Config.from_env().workers == 1


def test_target_chat_numeric(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_BOT_TOKEN", "1:x")
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "x")
    monkeypatch.setenv("TG_TARGET_CHAT", "-1001234567890")
    assert Config.from_env().target_chat == -1001234567890


def test_target_chat_username(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TG_BOT_TOKEN", "1:x")
    monkeypatch.setenv("TG_API_ID", "1")
    monkeypatch.setenv("TG_API_HASH", "x")
    monkeypatch.setenv("TG_TARGET_CHAT", "@my_channel")
    assert Config.from_env().target_chat == "@my_channel"


def test_socks_proxy_parsing() -> None:
    config = Config(proxy="socks5h://user:pa%40ss@127.0.0.1:1080")
    assert config.telethon_proxy() == {
        "proxy_type": "socks5",
        "addr": "127.0.0.1",
        "port": 1080,
        "rdns": True,
        "username": "user",
        "password": "pa@ss",
    }


def test_http_proxy_parsing() -> None:
    config = Config(proxy="http://127.0.0.1:7890")
    assert config.telethon_proxy() == {
        "proxy_type": "http",
        "addr": "127.0.0.1",
        "port": 7890,
        "rdns": False,
    }


def test_no_proxy() -> None:
    assert Config().telethon_proxy() is None


def test_invalid_proxy_scheme() -> None:
    with pytest.raises(ConfigError):
        Config(proxy="ftp://127.0.0.1:21").telethon_proxy()


def test_dotenv_is_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text(
        "TG_BOT_TOKEN=42:deadbeef\nTG_API_ID=42\nTG_API_HASH=deadbeef\nTG_SESSION=my-session\n",
        encoding="utf-8",
    )
    config = Config.from_env()
    assert config.bot_token == "42:deadbeef"
    assert config.api_id == 42
    assert config.api_hash == "deadbeef"
    assert config.session == "my-session"

"""配置：从环境变量 / .env 读取。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

MIB = 1024 * 1024
DEFAULT_MAX_FILE_SIZE = 2048 * MIB
PREMIUM_MAX_FILE_SIZE = 4096 * MIB


class ConfigError(RuntimeError):
    """配置缺失或非法。"""


def load_dotenv(path: Path | str = ".env") -> None:
    """极简 .env 加载器：KEY=VALUE，# 注释，支持引号，不覆盖已有环境变量。"""
    env_path = Path(path)
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value or default


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} 必须是整数，当前为 {raw!r}") from exc


def _env_path(name: str) -> Path | None:
    raw = _env(name)
    return Path(raw).expanduser() if raw else None


def _parse_id_set(raw: str) -> frozenset[int]:
    values: set[int] = set()
    for part in raw.replace(",", " ").split():
        try:
            values.add(int(part))
        except ValueError as exc:
            raise ConfigError(f"TG_ALLOWED_USERS 里的 {part!r} 不是数字 user id") from exc
    return frozenset(values)


def _parse_chat(raw: str) -> int | str | None:
    """目标聊天：数字（含 -100…）按 id 处理，否则按 @username 处理。"""
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return raw


@dataclass(slots=True)
class Config:
    bot_token: str = ""
    api_id: int = 0
    api_hash: str = ""
    session: str = "tg-sync-bot"
    download_dir: Path = field(default_factory=lambda: Path("downloads"))
    task_db: Path = field(default_factory=lambda: Path("state/tasks.db"))
    target_chat: int | str | None = None
    allowed_users: frozenset[int] = frozenset()
    keep_files: bool = False
    max_file_size: int = DEFAULT_MAX_FILE_SIZE
    status_messages: bool = True
    proxy: str | None = None
    ytdlp_cookies: Path | None = None
    ytdlp_cookies_from_browser: str | None = None
    log_level: str = "INFO"
    queue_size: int = 8
    album_limit: int = 10
    job_timeout: int = 3600
    workers: int = 3
    shutdown_grace: int = 300

    @classmethod
    def from_env(cls, *, require_credentials: bool = True) -> Config:
        load_dotenv()

        bot_token = _env("TG_BOT_TOKEN")
        api_id = _env_int("TG_API_ID", 0)
        api_hash = _env("TG_API_HASH")
        if require_credentials:
            missing = [
                name
                for name, value in (
                    ("TG_BOT_TOKEN", bot_token),
                    ("TG_API_ID", api_id),
                    ("TG_API_HASH", api_hash),
                )
                if not value
            ]
            if missing:
                raise ConfigError(
                    "缺少 " + " / ".join(missing) + "。\n"
                    "TG_BOT_TOKEN 来自 @BotFather；TG_API_ID / TG_API_HASH 来自 "
                    "https://my.telegram.org/apps（MTProto 上传大文件需要）"
                )
        if bot_token and ":" not in bot_token:
            raise ConfigError("TG_BOT_TOKEN 格式应为 123456:ABC-DEF…（@BotFather 给的完整 token）")

        max_mib = _env_int("TG_MAX_FILE_SIZE_MB", DEFAULT_MAX_FILE_SIZE // MIB)
        if max_mib <= 0:
            raise ConfigError("TG_MAX_FILE_SIZE_MB 必须大于 0")

        album_limit = _env_int("TG_ALBUM_LIMIT", 10)
        if not 1 <= album_limit <= 10:
            raise ConfigError("TG_ALBUM_LIMIT 必须在 1..10 之间（Telegram 相册上限为 10）")

        return cls(
            bot_token=bot_token,
            api_id=api_id,
            api_hash=api_hash,
            session=_env("TG_SESSION", "tg-sync-bot"),
            download_dir=Path(_env("TG_DOWNLOAD_DIR", "downloads")).expanduser(),
            task_db=Path(_env("TG_TASK_DB", "state/tasks.db")).expanduser(),
            target_chat=_parse_chat(_env("TG_TARGET_CHAT")),
            allowed_users=_parse_id_set(_env("TG_ALLOWED_USERS")),
            keep_files=_env_bool("TG_KEEP_FILES", False),
            max_file_size=max_mib * MIB,
            status_messages=_env_bool("TG_STATUS_MESSAGES", True),
            proxy=_env("TG_PROXY") or None,
            ytdlp_cookies=_env_path("TG_YTDLP_COOKIES"),
            ytdlp_cookies_from_browser=_env("TG_YTDLP_COOKIES_FROM_BROWSER") or None,
            log_level=_env("TG_LOG_LEVEL", "INFO").upper(),
            queue_size=max(1, _env_int("TG_QUEUE_SIZE", 8)),
            album_limit=album_limit,
            job_timeout=max(0, _env_int("TG_JOB_TIMEOUT", 3600)),
            workers=min(8, max(1, _env_int("TG_WORKERS", 3))),
            shutdown_grace=max(0, _env_int("TG_SHUTDOWN_GRACE", 300)),
        )

    def telethon_proxy(self) -> dict | None:
        """把 TG_PROXY 解析成 Telethon 需要的 proxy 字典。"""
        if not self.proxy:
            return None
        parts = urlsplit(self.proxy if "://" in self.proxy else f"socks5://{self.proxy}")
        scheme = (parts.scheme or "socks5").lower()
        if scheme not in {"socks5", "socks5h", "socks4", "http", "https"}:
            raise ConfigError(f"不支持的代理协议：{scheme}（可用 socks5 / socks5h / http）")
        if not parts.hostname:
            raise ConfigError(f"代理地址无效：{self.proxy}")
        proxy: dict = {
            "proxy_type": "socks5" if scheme.startswith("socks") else "http",
            "addr": parts.hostname,
            "port": parts.port or (1080 if scheme.startswith("socks") else 8080),
            "rdns": scheme.endswith("h"),
        }
        if parts.username:
            proxy["username"] = unquote(parts.username)
        if parts.password:
            proxy["password"] = unquote(parts.password)
        return proxy

    def ensure_dirs(self) -> None:
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.task_db.parent.mkdir(parents=True, exist_ok=True)

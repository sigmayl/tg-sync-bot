"""命令行入口。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from . import __version__
from .config import Config, ConfigError
from .downloaders import DownloadError, TelegramDownloader, XDownloader
from .links import LinkError, LinkKind, parse_link

MIB = 1024 * 1024


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tg-sync",
        description="Telegram 机器人：/sync <url> 下载媒体并回传（MTProto 大文件上传）",
    )
    parser.add_argument("--version", action="version", version=f"tg-sync {__version__}")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="启动机器人（默认）")
    sub.add_parser("login", help="用 bot token 登录并保存 session")

    download = sub.add_parser("download", help="只下载到本地，不上传（调试用）")
    download.add_argument("url", help="X 或 t.me 链接")
    download.add_argument(
        "-o", "--out", type=Path, default=None, help="输出目录（默认 downloads/manual）"
    )
    return parser


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _needs_telegram(url: str) -> bool:
    try:
        return parse_link(url).kind is LinkKind.TELEGRAM
    except LinkError:
        return False


def _friendly_auth_error(exc: Exception) -> str | None:
    """把 Telethon 的登录/鉴权异常翻译成可操作的提示。"""
    mapping = {
        "ApiIdInvalidError": "api_id / api_hash 无效，请检查 my.telegram.org 的配置",
        "ApiIdPublishedFloodError": "该 api_id 已被限制，请换一个应用",
        "AccessTokenInvalidError": "bot token 无效，请检查 @BotFather 给的 token",
        "AccessTokenExpiredError": "bot token 已失效，请重新生成",
        "AuthKeyUnregisteredError": "session 已失效，请删除对应的 .session 文件后重新登录",
    }
    return mapping.get(type(exc).__name__)


def _make_client(config: Config):
    from telethon import TelegramClient

    return TelegramClient(
        config.session, config.api_id, config.api_hash, proxy=config.telethon_proxy()
    )


async def _login(config: Config) -> int:
    client = _make_client(config)
    await client.start(bot_token=config.bot_token)
    me = await client.get_me()
    print(f"登录成功：@{me.username or '-'} ({me.first_name or 'bot'}, id={me.id})")
    print(f"session 已保存：{config.session}.session")
    print("下一步：给机器人发 /start，把回复里的 user id 填进 TG_ALLOWED_USERS")
    await client.disconnect()
    return 0


async def _download(config: Config, url: str, out: Path | None) -> int:
    config.ensure_dirs()
    link = parse_link(url)
    target = (out or config.download_dir / "manual").resolve()
    target.mkdir(parents=True, exist_ok=True)

    client = None
    try:
        if link.kind is LinkKind.X:
            downloader = XDownloader(
                cookies=config.ytdlp_cookies,
                cookies_from_browser=config.ytdlp_cookies_from_browser,
                proxy=config.proxy,
                max_file_size=config.max_file_size,
            )
        else:
            client = _make_client(config)
            await client.start(bot_token=config.bot_token)
            downloader = TelegramDownloader(client)

        items = await downloader.download(link, target)
    finally:
        if client is not None:
            await client.disconnect()

    for item in items:
        print(f"{item.path}  ({item.size / MIB:.1f} MiB)")
    print(f"共 {len(items)} 个文件 → {target}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    action = args.command or "run"

    require_credentials = action != "download" or _needs_telegram(args.url)
    try:
        config = Config.from_env(require_credentials=require_credentials)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2

    _setup_logging(config.log_level)

    try:
        if action == "login":
            return asyncio.run(_login(config))
        if action == "download":
            return asyncio.run(_download(config, args.url, args.out))

        from .app import SyncApp

        asyncio.run(SyncApp(config).run())
        return 0
    except (ConfigError, LinkError, DownloadError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        hint = _friendly_auth_error(exc)
        if hint is None:
            raise
        print(f"登录失败：{hint}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已退出", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

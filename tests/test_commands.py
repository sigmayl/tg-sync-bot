from __future__ import annotations

import pytest

from tg_sync.commands import CommandError, command_name, parse_command


def test_command_name() -> None:
    assert command_name("/sync@my_bot https://x.com/a/status/1") == "sync"
    assert command_name("/SYNC") == "sync"
    assert command_name("  /help") == "help"
    assert command_name("hello") is None
    assert command_name("https://x.com/a/status/1") is None
    assert command_name(None) is None


def test_single_url() -> None:
    command = parse_command("/sync https://x.com/NASA/status/1")
    assert command is not None
    assert command.urls == ("https://x.com/NASA/status/1",)


def test_multiple_urls_and_newlines() -> None:
    command = parse_command(
        "/sync https://x.com/a/status/1\nhttps://t.me/durov/2"
    )
    assert command is not None
    assert command.urls == ("https://x.com/a/status/1", "https://t.me/durov/2")


def test_bot_suffix_and_case() -> None:
    command = parse_command("/SYNC@my_bot https://x.com/a/status/1")
    assert command is not None
    assert command.urls == ("https://x.com/a/status/1",)


def test_extra_spaces() -> None:
    command = parse_command("   /sync    https://t.me/durov/1   ")
    assert command is not None
    assert command.urls == ("https://t.me/durov/1",)


@pytest.mark.parametrize("text", [None, "", "hello", "/other https://x.com/a/status/1", "https://x.com/a/status/1"])
def test_not_a_command(text: str | None) -> None:
    assert parse_command(text) is None


@pytest.mark.parametrize("text", ["/sync", "/sync   ", "/sync@bot"])
def test_missing_url(text: str) -> None:
    with pytest.raises(CommandError) as excinfo:
        parse_command(text)
    assert "/sync" in str(excinfo.value)


def test_too_many_urls() -> None:
    urls = " ".join(f"https://x.com/a/status/{i}" for i in range(11))
    with pytest.raises(CommandError):
        parse_command(f"/sync {urls}")


def test_force_download_flag() -> None:
    command = parse_command("/sync --download https://t.me/a/1")
    assert command is not None
    assert command.urls == ("https://t.me/a/1",)
    assert command.force_download is True

    short = parse_command("/sync -d https://t.me/a/1")
    assert short is not None and short.force_download is True


def test_unknown_flag_is_rejected() -> None:
    with pytest.raises(CommandError, match="未知参数"):
        parse_command("/sync --nope https://t.me/a/1")

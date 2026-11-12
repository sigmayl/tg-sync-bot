from __future__ import annotations

import pytest

from tg_sync.links import LinkError, LinkKind, parse_link


@pytest.mark.parametrize(
    ("raw", "expected_url"),
    [
        ("https://x.com/NASA/status/1234567890", "https://x.com/NASA/status/1234567890"),
        ("x.com/NASA/status/1234567890", "https://x.com/NASA/status/1234567890"),
        ("https://x.com/NASA/status/1234567890?s=20&t=abc", "https://x.com/NASA/status/1234567890"),
        ("https://twitter.com/NASA/statuses/1234567890", "https://x.com/NASA/status/1234567890"),
        ("https://mobile.twitter.com/NASA/status/1234567890", "https://x.com/NASA/status/1234567890"),
        ("https://fxtwitter.com/NASA/status/1234567890", "https://x.com/NASA/status/1234567890"),
        ("https://vxtwitter.com/NASA/status/1234567890", "https://x.com/NASA/status/1234567890"),
        ("https://x.com/NASA/status/1234567890/photo/2", "https://x.com/NASA/status/1234567890"),
        ("https://x.com/i/web/status/1234567890", "https://x.com/i/status/1234567890"),
        ("https://x.com/NASA/status/1234567890。", "https://x.com/NASA/status/1234567890"),
        ("<https://x.com/NASA/status/1234567890>", "https://x.com/NASA/status/1234567890"),
    ],
)
def test_x_links(raw: str, expected_url: str) -> None:
    link = parse_link(raw)
    assert link.kind is LinkKind.X
    assert link.url == expected_url
    assert link.tweet_id == 1234567890


def test_x_username_kept() -> None:
    link = parse_link("https://x.com/NASA/status/1234567890")
    assert link.username == "NASA"
    assert "X 推文" in link.describe()


def test_x_i_web_has_no_username() -> None:
    link = parse_link("https://x.com/i/web/status/1234567890")
    assert link.username is None


@pytest.mark.parametrize(
    ("raw", "username", "message_id"),
    [
        ("https://t.me/durov/123", "durov", 123),
        ("t.me/durov/123", "durov", 123),
        ("https://t.me/s/durov/123?single", "durov", 123),
        ("https://telegram.me/durov/123", "durov", 123),
    ],
)
def test_telegram_public_links(raw: str, username: str, message_id: int) -> None:
    link = parse_link(raw)
    assert link.kind is LinkKind.TELEGRAM
    assert link.username == username
    assert link.channel_id is None
    assert link.message_id == message_id
    assert link.url == f"https://t.me/{username}/{message_id}"


def test_telegram_private_link() -> None:
    link = parse_link("https://t.me/c/1234567890/55")
    assert link.kind is LinkKind.TELEGRAM
    assert link.channel_id == -1001234567890
    assert link.message_id == 55
    assert link.username is None


@pytest.mark.parametrize(
    "raw",
    [
        "https://t.me/durov",
        "https://t.me/+AbCdEfGhIjK",
        "https://t.me/joinchat/AbCdEfGhIjK",
        "https://t.me/addstickers/PackName",
        "https://example.com/foo/bar",
        "https://x.com/NASA",
        "https://x.com/NASA/status/notanumber",
        "https://t.me/durov/abc",
        "",
    ],
)
def test_unsupported_links(raw: str) -> None:
    with pytest.raises(LinkError):
        parse_link(raw)


def test_telegram_describe() -> None:
    assert "私有频道" in parse_link("https://t.me/c/999/1").describe()
    assert "@durov" in parse_link("https://t.me/durov/1").describe()

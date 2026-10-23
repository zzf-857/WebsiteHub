"""Q51 图片来源、链接边界与拉取后的 API 契约。"""

from __future__ import annotations

from html import escape

import feedparser
import pytest

from webhub.feeds.parser import MAX_URL_LENGTH, parse_feed

from .test_feeds import ORIGIN, Environment, _add, _items, _ok, _register, _use
from .test_feeds import environment as environment

SOURCES = [
    '<media:thumbnail url="{url}"/>',
    '<media:content url="{url}" medium="image"/>',
    '<itunes:image href="{url}"/>',
    '<enclosure url="{url}" type="image/png"/>',
    '<description><![CDATA[<p>摘要<img src="{url}"></p>]]></description>',
]


def image_feed(*, entry: str = "", channel: str = "") -> bytes:
    return (
        '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/" '
        'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd" '
        'xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        f"<channel><title>图片订阅</title>{channel}<item><guid>image-item</guid>"
        f"<title>图片条目</title><link>https://images.example/article</link>{entry}"
        "</item></channel></rss>"
    ).encode()


@pytest.mark.parametrize("source", SOURCES)
def test_item_image_reads_each_supported_source(source: str) -> None:
    url = "https://images.example/cover.png"
    assert parse_feed(image_feed(entry=source.format(url=url))).items[0].image_url == url


@pytest.mark.parametrize("first", range(len(SOURCES)))
def test_item_image_priority(first: int) -> None:
    entry = "".join(
        source.format(url=f"https://images.example/{index}.png")
        for index, source in enumerate(SOURCES)
        if index >= first
    )
    assert parse_feed(image_feed(entry=entry)).items[0].image_url == (
        f"https://images.example/{first}.png"
    )


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "file:///secret.png",
        "https://user:pass@images.example/a.png",
        "https://images.example/" + "a" * MAX_URL_LENGTH,
    ],
)
def test_item_image_rejects_unsafe_or_overlong_links(source: str, url: str) -> None:
    assert (
        parse_feed(image_feed(entry=source.format(url=escape(url, quote=True)))).items[0].image_url
        is None
    )


def test_image_type_content_html_and_invalid_priority_fallback() -> None:
    entry = (
        '<media:thumbnail url="javascript:bad"/>'
        '<media:content url="https://images.example/video" type="video/mp4"/>'
        '<media:content url="https://images.example/type.png" type="image/png"/>'
    )
    assert parse_feed(image_feed(entry=entry)).items[0].image_url == (
        "https://images.example/type.png"
    )
    entry = (
        '<content:encoded><![CDATA[<img src="https://images.example/content.png">'
        '<img src="https://images.example/second.png">]]></content:encoded>'
        '<description><![CDATA[<img src="https://images.example/summary.png">]]></description>'
    )
    assert parse_feed(image_feed(entry=entry)).items[0].image_url == (
        "https://images.example/content.png"
    )


def test_itunes_image_alias_and_channel_rejects_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        feedparser,
        "parse",
        lambda _document: {
            "version": "rss20",
            "feed": {"itunes_image": "https://images.example/feed.png"},
            "entries": [
                {
                    "id": "alias",
                    "title": "Alias",
                    "itunes_image": {"href": "https://images.example/item.png"},
                }
            ],
        },
    )
    parsed = parse_feed(b"alias")
    assert parsed.image_url == "https://images.example/feed.png"
    assert parsed.items[0].image_url == "https://images.example/item.png"
    monkeypatch.undo()
    parsed = parse_feed(
        image_feed(channel="<image><url>https://u:p@images.example/a</url></image>")
    )
    assert parsed.image_url is None


def test_refresh_updates_feed_image_and_exposes_item_image(environment: Environment) -> None:
    env = environment
    _use(env.client, _register(env.client, "image-reader"))
    url = "https://images.example/rss"
    env.fetcher.queue(
        url,
        _ok(
            image_feed(
                entry=SOURCES[0].format(url="https://images.example/item.png"),
                channel="<image><url>https://images.example/feed.png</url></image>",
            )
        ),
    )
    feed = _add(env, url)
    assert feed["image_url"] == "https://images.example/feed.png"
    item = _items(env)[0]
    assert item["image_url"] == "https://images.example/item.png"
    assert (
        env.client.get("/api/feeds/picks").json()["items"][0]["item"]["image_url"]
        == (item["image_url"])
    )
    env.fetcher.queue(
        url,
        _ok(
            image_feed(
                channel='<itunes:image href="https://images.example/updated.png"/>',
            )
        ),
    )
    response = env.client.post(f"/api/feeds/{feed['id']}/refresh", headers=ORIGIN)
    assert response.status_code == 200
    assert response.json()["image_url"] == "https://images.example/updated.png"
    assert env.client.get("/api/feeds").json()["items"][0]["image_url"] == (
        "https://images.example/updated.png"
    )
    env.fetcher.queue(url, _ok(image_feed()))
    assert (
        env.client.post(f"/api/feeds/{feed['id']}/refresh", headers=ORIGIN).json()["image_url"]
        is None
    )

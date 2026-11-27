"""Pixel: design requests, options, picking, changes, handing off to Nova, logo, and the locked-down renderer."""

from __future__ import annotations

import io
import os
from datetime import datetime
from pathlib import Path

import pytest
import time_machine
from PIL import Image

from app import clock
from app.pixel.render import Renderer, _clean
from app.pixel.service import Pixel
from tests.test_nova import jpeg
from tests.test_nova import make as make_nova

NOW = datetime(2026, 11, 27, 16, 0, tzinfo=clock.TZ)
CHAT = {"id": 9229985118}


@pytest.fixture
def frozen():
    with time_machine.travel(NOW, tick=False) as t:
        yield t


class FakeDesigner:
    def __init__(self):
        self.calls = []

    async def design(self, request, assets, *, has_logo, previous=None, feedback="", taste=None):
        self.calls.append({"request": request, "assets": [a["name"] for a in assets], "has_logo": has_logo,
                           "previous": previous, "feedback": feedback, "taste": taste})
        page = lambda f, c: {"format": f, "html": f"<style>body{{background:{c}}}</style><h1>$20 OFF</h1>"}  # noqa
        return {"kind": "poster", "title": "Black Friday poster", "formats": ["feed", "story"],
                "options": [{"label": "Bold photo", "pages": [page("feed", "#123"), page("story", "#123")]},
                            {"label": "Clean graphic", "pages": [page("feed", "#fff"), page("story", "#fff")]}]}


class FakeRenderer:
    def __init__(self):
        self.calls = []

    async def render(self, body, fmt, assets):
        self.calls.append((fmt, sorted(assets)))
        return jpeg(size=(1080, 1350) if fmt == "feed" else (1080, 1920))


def make(tmp_path, files=None):
    w, nova, vision, pub = make_nova(tmp_path / "nova", [])
    files = files or {}

    async def download(fid):
        return files[fid]

    px = Pixel(w.dex, FakeDesigner(), FakeRenderer(), nova.host, str(tmp_path / "pixel"), bot_token="t",
               nova=nova, batch_seconds=0, downloader=download, frame_reader=lambda b: [jpeg(), jpeg()])
    w.bot.pixel = px
    return w, px, nova, pub


async def settle(px):
    await px.debouncer.drain()


def buttons(w):
    return [m for m in w.owner.messages if m["kind"] == "buttons"]


async def test_design_request_with_album_then_pick(frozen, tmp_path):
    w, px, nova, _ = make(tmp_path, {"a": jpeg(), "b": jpeg()})
    await w.bot.handle_update({"message": {"chat": CHAT, "photo": [{"file_id": "a"}], "media_group_id": "g1",
                                           "caption": "/design Black Friday poster, $20 off, book by 11/30"}})
    await w.bot.handle_update({"message": {"chat": CHAT, "photo": [{"file_id": "b"}], "media_group_id": "g1"}})
    await settle(px)
    assert nova.list() == []  # album went to Pixel, not Nova
    call = px.designer.calls[0]
    assert call["request"] == "Black Friday poster, $20 off, book by 11/30" and call["assets"] == ["photo1", "photo2"]
    album = [m for m in w.owner.messages if m["kind"] == "media"][-1]
    assert len(album["media"]) == 2 and "A: Bold photo" in album["text"]
    card = buttons(w)[-1]
    assert card["buttons"][0] == [("✅ Use A", "D0001:pick0"), ("✅ Use B", "D0001:pick1")]
    await w.bot.handle_update({"callback_query": {"id": "1", "data": "D0001:pick1",
                                                  "message": {"chat": CHAT, "message_id": 4}}})
    docs = [m for m in w.owner.messages if m["kind"] == "document"]
    assert [d["text"] for d in docs] == ["D0001 · Feed post 4:5", "D0001 · Story / Reel cover 9:16"]
    assert buttons(w)[-1]["buttons"][0] == [("📤 Post with Nova", "D0001:nova")]
    await w.dex.activity.drain()
    assert w.rows("Activity")[0]["agent"] == "Pixel"


async def test_text_only_request_change_and_new_ideas(frozen, tmp_path):
    w, px, _, _ = make(tmp_path)
    assert "Tell Pixel what you need" in await w.bot.handle_text("/design")
    assert await w.bot.handle_text("/design YouTube thumbnail: Interior Transformation") is None
    await settle(px)
    assert px.get("D0001")["status"] == "choosing"
    await px.handle_callback("D0001", "edit")
    assert "what should change" in w.owner.texts("ask")[-1]
    assert await w.bot.handle_text("bigger text, darker blue") is None
    last = px.designer.calls[-1]
    assert last["feedback"] == "bigger text, darker blue" and last["previous"]["label"] == "Bold photo"
    assert px.get("D0001")["data"]["round"] == 2
    await px.handle_callback("D0001", "again")
    assert "different" in px.designer.calls[-1]["feedback"]


async def test_post_with_nova_uses_feed_and_story_images(frozen, tmp_path):
    w, px, nova, pub = make(tmp_path)
    await px.request("Black Friday poster, $20 off any detail")
    await settle(px)
    await px.handle_callback("D0001", "pick0")
    await px.handle_callback("D0001", "nova")
    post = nova.get("N0001")
    files = px.get("D0001")["data"]["options"][0]["pages"]
    assert post["data"]["media"] == [files[0]["file"]] and post["data"]["story_media"] == [files[1]["file"]]
    assert post["data"]["targets"] == ["igf", "fbf", "igs", "fbs"] and post["data"]["offer"]
    assert "OFFER POST" in buttons(w)[-1]["text"]
    await nova.handle_callback("N0001", "now")
    slugs = pub.slugs()
    assert "INSTAGRAM_CREATE_MEDIA_CONTAINER" in slugs and "FACEBOOK_CREATE_MULTI_PHOTO_POST" in slugs
    stories = [a for s, a in pub.calls if s == "INSTAGRAM_CREATE_MEDIA_CONTAINER" and a.get("media_type") == "STORIES"]
    assert len(stories) == 1 and stories[0]["image_url"].endswith(files[1]["file"])


async def test_video_thumbnail_uses_frames(frozen, tmp_path):
    w, px, _, _ = make(tmp_path, {"v": b"video"})
    await w.bot.handle_update({"message": {"chat": CHAT, "video": {"file_id": "v", "file_size": 100},
                                           "caption": "/design thumbnail: Muddy Truck Interior Rescue"}})
    await settle(px)
    assert px.designer.calls[0]["assets"] == ["video1_frame1", "video1_frame2"]


async def test_logo_saved_and_used(frozen, tmp_path):
    logo = io.BytesIO()
    Image.new("RGBA", (400, 200), (0, 80, 255, 255)).save(logo, "PNG")
    w, px, _, _ = make(tmp_path, {"L": logo.getvalue()})
    await w.bot.handle_update({"message": {"chat": CHAT, "caption": "/logo",
                                           "document": {"file_id": "L", "mime_type": "image/png"}}})
    assert px.logo and any("Logo saved" in t for t in w.owner.texts("notify"))
    await px.request("poster")
    await settle(px)
    assert px.designer.calls[0]["has_logo"] and "logo" in px.renderer.calls[0][1]


async def test_regular_photos_still_go_to_nova(frozen, tmp_path):
    w, px, nova, _ = make(tmp_path, {"x": jpeg()})
    assert not px.wants({"photo": [{"file_id": "x"}], "caption": "Tahoe, Kyle"})
    assert px.wants({"photo": [{"file_id": "x"}], "caption": "/design poster"})


def test_html_is_sanitized():
    dirty = ('<style>@import url(http://x);h1{color:red}</style><h1 onclick="x()">Hi</h1>'
             '<script>alert(1)</script><iframe src="http://x"></iframe><link rel=stylesheet href=http://x>')
    clean = _clean(dirty)
    assert "<script" not in clean and "onclick" not in clean and "<iframe" not in clean and "@import" not in clean
    assert "<link" not in clean and "<h1" in clean


CHROME = os.environ.get("CHROMIUM_PATH") or "/opt/pw-browsers/chromium"


@pytest.mark.skipif(not Path(CHROME).exists(), reason="no local Chromium")
async def test_real_render_blocks_network_and_sizes(tmp_path):
    photo = tmp_path / "p.jpg"
    photo.write_bytes(jpeg((10, 120, 250), (1600, 1200)))
    r = Renderer(CHROME)
    try:
        body = ("<style>.bg{position:absolute;inset:0;background:url('asset:p') center/cover}</style>"
                "<div class=bg></div><img src='http://169.254.169.254/latest/meta-data'>"
                "<h1 style=\"position:absolute;top:300px;font:400 120px 'Anton';color:#fff\">$20 OFF</h1>")
        out = Image.open(io.BytesIO(await r.render(body, "youtube", {"p": photo})))
        assert out.size == (1280, 720)
        assert out.getpixel((5, 5))[2] > 150  # the local photo loaded (blue)
    finally:
        await r.close()



async def test_pixel_remembers_taste(frozen, tmp_path):
    w, px, nova, _ = make(tmp_path)
    assert "hasn't learned" in await w.bot.handle_text("/style")
    assert "always: dark navy background" in await w.bot.handle_text("/design remember: dark navy background")
    await px.request("Black Friday poster")
    await settle(px)
    assert px.designer.calls[-1]["taste"]["rules"] == ["dark navy background"]
    await px.handle_callback("D0001", "pick1")  # likes "Clean graphic"
    await px.handle_callback("D0001", "edit")
    await w.bot.handle_text("make the price huge")
    await px.handle_callback("D0001", "pick0")
    await px.handle_callback("D0001", "nova")  # posted -> strongest signal
    await px.request("Cyber Monday poster")
    await settle(px)
    taste = px.designer.calls[-1]["taste"]
    assert taste["feedback"] == ["make the price huge"]
    assert taste["liked"][0]["used"] and taste["liked"][0]["job"] == "D0001"
    assert taste["liked"][0].get("jpeg")  # a saved picture of the liked design goes to the designer
    text = await w.bot.handle_text("/style")
    assert "dark navy background" in text and "make the price huge" in text and "posted" in text
    assert "forgot" in await w.bot.handle_text("/style forget")
    assert px.taste() == {"rules": [], "feedback": [], "liked": []}


async def test_designer_prompt_includes_taste():
    from app.pixel.designer import Designer

    class Client:
        def __init__(self):
            self.messages = self
            self.sent = None

        async def create(self, **kw):
            self.sent = kw
            from types import SimpleNamespace
            body = ('{"kind":"poster","title":"t","formats":["feed"],"options":[{"label":"x","pages":'
                    '[{"format":"feed","html":"<h1>x</h1>"}]}]}')
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=body)])

    c = Client()
    await Designer(c, "m").design("poster", [], has_logo=False, taste={
        "rules": ["navy background"], "feedback": ["bigger price"],
        "liked": [{"kind": "poster", "label": "Bold", "format": "feed", "html": "<h1>LIKED</h1>", "used": True,
                   "jpeg": jpeg()}]})
    blocks = c.sent["messages"][0]["content"]
    text = " ".join(b.get("text", "") for b in blocks)
    assert "Rule: navy background" in text and "bigger price" in text and "<h1>LIKED</h1>" in text
    assert "they posted this one" in text and any(b["type"] == "image" for b in blocks)

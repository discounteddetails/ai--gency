"""Nova: media privacy, caption rules, approval flow, scheduling and publishing (docs/NOVA_SPEC.md)."""

from __future__ import annotations

import io
import os
import time
from datetime import datetime, timedelta

import pytest
import time_machine
from PIL import Image

from app import clock
from app.nova import captions
from app.nova.media import Box, MediaHost, blur_boxes, fit_feed, open_image, to_jpeg
from app.nova.service import Nova, find_id
from tests.fakes import World

NOW = datetime(2026, 12, 1, 10, 0, tzinfo=clock.TZ)  # Tue 10:00 AM
GOOD = ("This 2021 Tahoe came back looking brand new inside! 🐶✨ Pet hair gone, seats shampooed. "
        "DM 'DETAIL' for a price on yours.\n#kyletx #buda #austindetailing #mobiledetailing #cardetailing")


@pytest.fixture
def frozen():
    with time_machine.travel(NOW, tick=False) as t:
        yield t


def jpeg(color=(200, 30, 30), size=(800, 600)) -> bytes:
    img = Image.new("RGB", size, color)
    for x in range(0, size[0], 20):  # some detail so blur is measurable
        for y in range(size[1]):
            img.putpixel((x, y), (255, 255, 255))
    return to_jpeg(img)


def item(category, quality=8, **kw):
    return {"category": category, "quality": quality, "vehicle": kw.get("vehicle", "black Chevy Tahoe"),
            "area": kw.get("area", "interior"), "plates": kw.get("plates", []), "house_numbers": [],
            "faces": kw.get("faces", []), "other_private": "", "pair_key": kw.get("pair_key", "")}


class FakeVision:
    def __init__(self, results, captions_=None):
        self.results, self.captions = results, list(captions_ or [GOOD])
        self.caption_calls = []

    async def classify(self, items):
        assert len(items) == len(self.results)
        return [{"index": i, **r} for i, r in enumerate(self.results)]

    async def caption(self, facts, recent, photos):
        self.caption_calls.append(facts)
        return self.captions.pop(0) if self.captions else GOOD


class PublishLog:
    """Composio stand-in for Nova's publish tools."""

    def __init__(self, world: World, fail: set[str] | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.fail = fail or set()
        self._orig = world.composio.execute
        world.composio.execute = self.execute

    async def execute(self, slug, args):
        if not slug.startswith(("INSTAGRAM_CREATE", "INSTAGRAM_POST", "FACEBOOK_UPLOAD", "FACEBOOK_PUBLISH",
                                "FACEBOOK_CREATE")):
            return await self._orig(slug, args)
        self.calls.append((slug, args))
        if slug in self.fail:
            from app.services.composio_client import ComposioError
            raise ComposioError("permission denied")
        n = len(self.calls)
        return {"data": {"id": f"id{n}"}} if "PUBLISH" not in slug else {"id": f"pub{n}"}

    def slugs(self):
        return [s for s, _ in self.calls]


def make(tmp_path, results, files=None, captions_=None):
    w = World()
    data = files or {f"f{i}": jpeg() for i in range(len(results))}

    async def download(fid):
        return data[fid]

    vision = FakeVision(results, captions_)
    nova = Nova(w.dex, vision, MediaHost(str(tmp_path), "https://dex.example"), bot_token="t",
                ig_user_id="IG1", page_id="PG1", batch_seconds=0, downloader=download,
                frame_reader=lambda b: [jpeg(), jpeg()])
    w.bot.nova = nova
    return w, nova, vision, PublishLog(w)


def photo_msg(fid, caption=""):
    return {"chat": {"id": 9229985118}, "photo": [{"file_id": "small"}, {"file_id": fid}], "caption": caption}


async def send(w, nova, msgs):
    for m in msgs:
        await w.bot.handle_update({"message": m})
    await nova.debouncer.drain()


def cards(w):
    return [m for m in w.owner.messages if m["kind"] == "buttons"]


# ------------------------------------------------------------------ media + captions
def test_blur_only_touches_the_box():
    img = open_image(jpeg())
    out = blur_boxes(img, [Box(100, 100, 300, 300)])
    assert out.getpixel((700, 500)) == img.getpixel((700, 500))  # outside: unchanged
    inside = [out.getpixel((x, 150)) for x in range(160, 260)]
    assert inside != [img.getpixel((x, 150)) for x in range(160, 260)]  # inside: smoothed


def test_fit_feed_aspect_and_size():
    tall = fit_feed(Image.new("RGB", (1000, 3000)))
    assert abs(tall.size[0] / tall.size[1] - 0.8) < 0.01 and max(tall.size) <= 1440
    wide = fit_feed(Image.new("RGB", (4000, 1000)))
    assert wide.size[0] / wide.size[1] <= 1.91 + 0.01


def test_media_host_paths_and_cleanup(tmp_path):
    h = MediaHost(str(tmp_path), "https://x/")
    a, b = h.save(b"1", "jpg"), h.save(b"2", "jpg")
    assert h.url(a) == f"https://x/media/{a}" and h.path(a)
    assert h.path("../etc/passwd") is None and h.path("nope.jpg") is None
    old = time.time() - 10 * 86400
    for n in (a, b):
        os.utime(tmp_path / n, (old, old))
    assert h.cleanup(7, keep={a}) == 1 and h.path(a) and not h.path(b)


@pytest.mark.parametrize("bad", ["Looks perfect now!", "Flawless finish", "Only $99 today", "100% clean",
                                 "We were at 412 Elm St today", "Guaranteed shine"])
def test_caption_blocks(bad):
    with pytest.raises(captions.CaptionRejected):
        captions.check(bad)


def test_caption_keeps_five_hashtags_and_allows_new():
    c = captions.check("Brand new look ✨ DM us #a #b #c #d #e #f #g")
    assert c.count("#") == 5 and "#f" not in c and "new" in c


def test_find_id_nested():
    assert find_id({"data": {"response_data": {"id": "123"}}}, ("id",)) == "123"
    assert find_id({"x": 1}, ("id",)) == ""


# ------------------------------------------------------------------ flow
async def test_batch_previews_then_story_posts_only_after_approval(frozen, tmp_path):
    w, nova, _, pub = make(tmp_path, [item("on_the_way"), item("before", pair_key="seat"),
                                      item("after", pair_key="seat", plates=[{"x0": 10, "y0": 10, "x1": 90,
                                                                              "y1": 40}]),
                                      item("skip", quality=2)])
    await send(w, nova, [photo_msg(f"f{i}", "Tahoe, Kyle" if i == 0 else "") for i in range(4)])
    assert "Keep sending" in w.owner.texts("notify")[0]
    assert pub.calls == []  # nothing posts without approval
    story, post = cards(w)
    assert "STORY" in story["text"] and story["buttons"][0][0] == ("✅ Post Story", "N0001:post")
    assert "BEFORE/AFTER POST" in post["text"] and "6:30" in post["text"] and GOOD in post["text"]
    assert any("skipped 1" in t for t in w.owner.texts("notify"))
    assert nova.vision.caption_calls and "Tahoe, Kyle" in nova.vision.caption_calls[0]
    # the preview images are the processed files that will be posted
    assert all(nova.host.path(u.rsplit("/", 1)[1]) for u in story["media"])

    await w.bot.handle_update({"callback_query": {"id": "1", "data": "N0001:post",
                                                  "message": {"chat": {"id": 9229985118}, "message_id": 5}}})
    assert pub.slugs() == ["INSTAGRAM_CREATE_MEDIA_CONTAINER", "INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH",
                           "FACEBOOK_UPLOAD_PHOTO", "FACEBOOK_PUBLISH_PAGE_PHOTO_STORY"]
    assert pub.calls[0][1]["media_type"] == "STORIES" and pub.calls[0][1]["image_url"].startswith("https://dex.example/media/")
    assert pub.calls[1][1]["creation_id"] == "id1"
    assert pub.calls[2][1]["published"] is False and pub.calls[3][1]["photo_id"] == "id3"
    assert nova.get("N0001")["status"] == "published"
    assert any("live on Instagram + Facebook" in t for t in w.owner.texts("notify"))
    # second tap does nothing
    await w.bot.handle_update({"callback_query": {"id": "2", "data": "N0001:post",
                                                  "message": {"chat": {"id": 9229985118}, "message_id": 5}}})
    assert len(pub.calls) == 4
    rows = {r["content_id"]: r for r in w.rows("Content")}
    assert rows["N0001"]["status"] == "published" and rows["N0002"]["status"] == "pending"


async def test_posts_schedule_one_per_evening_and_publish(frozen, tmp_path):
    w, nova, _, pub = make(tmp_path, [item("before"), item("after"), item("after")])
    await send(w, nova, [photo_msg(f"f{i}") for i in range(3)])
    r1 = await nova.handle_callback("N0001", "post")
    assert "6:30" in r1 and nova.get("N0001")["status"] == "scheduled"
    # a second approved post on the same day spills to tomorrow
    nova._save("N0009", "post", "pending", {"media": nova.get("N0001")["data"]["media"], "caption": GOOD})
    r2 = await nova.handle_callback("N0009", "post")
    s1 = clock.parse_iso(nova.get("N0001")["data"]["scheduled_for"])
    s2 = clock.parse_iso(nova.get("N0009")["data"]["scheduled_for"])
    assert (s1.hour, s1.minute, s1.date()) == (18, 30, NOW.date())
    assert s2.date() == NOW.date() + timedelta(days=1) and "N0009" in r2

    assert await nova.publish_due() == 0 and pub.calls == []
    frozen.move_to(NOW.replace(hour=18, minute=31))
    assert await nova.publish_due() == 1
    assert pub.slugs() == ["INSTAGRAM_CREATE_CAROUSEL_CONTAINER", "INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH",
                           "FACEBOOK_CREATE_MULTI_PHOTO_POST"]
    carousel = pub.calls[0][1]
    assert len(carousel["child_image_urls"]) == 3 and carousel["caption"] == GOOD
    assert pub.calls[2][1]["message"] == GOOD and len(pub.calls[2][1]["photo_urls"]) == 3
    assert nova.get("N0001")["status"] == "published" and nova.get("N0009")["status"] == "scheduled"


async def test_after_8pm_posts_go_to_tomorrow(tmp_path):
    with time_machine.travel(NOW.replace(hour=20, minute=30), tick=False):
        w, nova, _, _ = make(tmp_path, [])
        slot = nova.next_slot()
        assert slot.date() == NOW.date() + timedelta(days=1) and (slot.hour, slot.minute) == (18, 30)


async def test_edit_caption_rejects_perfect_then_accepts(frozen, tmp_path):
    w, nova, _, _ = make(tmp_path, [item("before"), item("after")])
    await send(w, nova, [photo_msg("f0"), photo_msg("f1")])
    await w.bot.handle_update({"callback_query": {"id": "1", "data": "N0001:edit",
                                                  "message": {"chat": {"id": 9229985118}, "message_id": 9}}})
    assert "send the new caption" in w.owner.texts("ask")[0]
    reply = await w.bot.handle_text("Perfect shine on this Tahoe!")
    assert "can't be used" in reply
    assert await w.bot.handle_text("Fresh new shine on this Tahoe ✨ DM us #kyletx") is None
    assert nova.get("N0001")["data"]["caption"].startswith("Fresh new shine")
    assert "Fresh new shine" in cards(w)[-1]["text"]


async def test_bad_model_caption_falls_back_and_flags(frozen, tmp_path):
    w, nova, _, _ = make(tmp_path, [item("before"), item("after")],
                         captions_=["So perfect! $150", "Still perfect"])
    await send(w, nova, [photo_msg("f0"), photo_msg("f1")])
    c = nova.get("N0001")["data"]["caption"]
    assert "perfect" not in c.lower() and "$" not in c and c.count("#") == 5
    assert "Please edit the caption" in cards(w)[-1]["text"]


async def test_video_story_instagram_only_with_warning(frozen, tmp_path):
    w, nova, _, pub = make(tmp_path, [item("mid_job", plates=[{"x0": 1, "y0": 1, "x1": 5, "y1": 5}])],
                           files={"v": b"fakevideo"})
    await w.bot.handle_update({"message": {"chat": {"id": 9229985118}, "video": {"file_id": "v", "file_size": 10}}})
    await nova.debouncer.drain()
    card = cards(w)[0]
    assert card["media_kind"] == "video" and "license plate" in card["text"] and "Instagram Story" in card["text"]
    await nova.handle_callback("N0001", "post")
    assert pub.slugs() == ["INSTAGRAM_CREATE_MEDIA_CONTAINER", "INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH"]
    assert "video_url" in pub.calls[0][1]


async def test_big_video_rejected(frozen, tmp_path):
    w, nova, _, _ = make(tmp_path, [], files={})
    await w.bot.handle_update({"message": {"chat": {"id": 9229985118},
                                           "video": {"file_id": "v", "file_size": 50 * 1024 * 1024}}})
    await nova.debouncer.drain()
    assert any("20 MB" in t for t in w.owner.texts("notify"))


async def test_partial_failure_is_reported(frozen, tmp_path):
    w, nova, _, pub = make(tmp_path, [item("on_the_way")])
    pub.fail = {"FACEBOOK_UPLOAD_PHOTO"}
    await send(w, nova, [photo_msg("f0")])
    msg = await nova.handle_callback("N0001", "post")
    assert "posted on Instagram" in msg and "Facebook: permission denied" in msg
    assert nova.get("N0001")["status"] == "published"


async def test_faces_blurred_in_posts(frozen, tmp_path):
    files = {"f0": jpeg(), "f1": jpeg()}
    w, nova, _, _ = make(tmp_path, [item("before"), item("after", faces=[{"x0": 0, "y0": 0, "x1": 500,
                                                                          "y1": 500}])], files=files)
    await send(w, nova, [photo_msg("f0"), photo_msg("f1")])
    after_name = nova.get("N0001")["data"]["media"][1]
    posted = Image.open(io.BytesIO(nova.host.path(after_name).read_bytes()))
    original = fit_feed(open_image(files["f1"]))
    row = [posted.getpixel((x, 50)) for x in range(100, 200)]
    assert row != [original.getpixel((x, 50)) for x in range(100, 200)]


async def test_media_ignored_from_other_chats(frozen, tmp_path):
    w, nova, _, _ = make(tmp_path, [item("after")])
    await w.bot.handle_update({"message": {"chat": {"id": 1}, "photo": [{"file_id": "f0"}]}})
    await nova.debouncer.drain()
    assert nova.list() == []


async def test_text_note_during_batch_goes_to_nova(frozen, tmp_path):
    w, nova, vision, _ = make(tmp_path, [item("before"), item("after")])
    nova.debouncer.seconds = 0.05
    await w.bot.handle_update({"message": photo_msg("f0")})
    await w.bot.handle_update({"message": {"chat": {"id": 9229985118}, "text": "Silver F-150 in Buda"}})
    await w.bot.handle_update({"message": photo_msg("f1")})
    await nova.debouncer.drain()
    assert any("Added that note" in t for t in w.owner.texts("notify"))
    assert "Silver F-150 in Buda" in vision.caption_calls[0]
    assert not nova.collecting()

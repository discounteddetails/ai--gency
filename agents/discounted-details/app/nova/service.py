"""Nova: Social Media agent (docs/NOVA_SPEC.md). Owner sends photos/videos to the Telegram bot -> Nova classifies,
blurs private details, previews Stories and slideshow posts in Telegram -> owner approves -> Nova posts."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from app import clock
from app.core import Dex
from app.nova import captions
from app.nova.media import (
    Box,
    MediaError,
    MediaHost,
    blur_boxes,
    cap_size,
    fit_feed,
    for_vision,
    open_image,
    run_blocking,
    telegram_download,
    to_jpeg,
    video_frames,
)
from app.services.composio_client import ComposioError
from app.services.debounce import Debouncer
from app.services.sheets import _cell

log = logging.getLogger(__name__)
POST_HOUR, POST_MINUTE, LAST_POST_HOUR = 18, 30, 20
MIN_QUALITY = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS nova_content (content_id TEXT PRIMARY KEY, kind TEXT, status TEXT, data_json TEXT,
  created_at TEXT, scheduled_for TEXT);
"""


def find_id(data: Any, keys: tuple[str, ...]) -> str:
    """Composio wraps Graph responses differently per tool; find the first matching id anywhere in the payload."""
    if isinstance(data, dict):
        for k in keys:
            if data.get(k):
                return str(data[k])
        for v in data.values():
            if found := find_id(v, keys):
                return found
    elif isinstance(data, list):
        for v in data:
            if found := find_id(v, keys):
                return found
    return ""


class Nova:
    def __init__(self, dex: Dex, vision, host: MediaHost, *, bot_token: str, ig_user_id: str, page_id: str,
                 batch_seconds: float = 180, downloader=None, frame_reader=None):
        self.dex, self.vision, self.host = dex, vision, host
        self.token, self.ig_user_id, self.page_id = bot_token, ig_user_id, page_id
        self.debouncer = Debouncer(batch_seconds, self._process_batch)
        self.download = downloader or (lambda fid: telegram_download(self.token, fid))
        self.frames = frame_reader or video_frames
        with dex.db._lock:
            dex.db._conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ storage
    def _save(self, cid: str, kind: str, status: str, data: dict[str, Any]) -> None:
        with self.dex.db._lock:
            self.dex.db._conn.execute(
                "INSERT OR REPLACE INTO nova_content VALUES (?,?,?,?,?,?)",
                (cid, kind, status, json.dumps(data), data.get("created_at") or clock.now().isoformat(),
                 data.get("scheduled_for")))

    def get(self, cid: str) -> dict[str, Any] | None:
        rows = self.dex.db._q("SELECT * FROM nova_content WHERE content_id=?", (cid,))
        if not rows:
            return None
        r = dict(rows[0])
        r["data"] = json.loads(r.pop("data_json"))
        return r

    def list(self, status: str | None = None, kind: str | None = None) -> list[dict[str, Any]]:
        rows = self.dex.db._q("SELECT content_id FROM nova_content ORDER BY created_at")
        out = [c for r in rows if (c := self.get(r["content_id"]))]
        return [c for c in out if (status is None or c["status"] == status) and (kind is None or c["kind"] == kind)]

    async def _log(self, cid: str, kind: str, status: str, data: dict[str, Any]) -> None:
        fields = {"kind": kind, "status": status, "scheduled_for": data.get("scheduled_for", ""),
                  "caption": data.get("caption", ""), "platforms": ", ".join(data.get("platforms", [])),
                  "ig_media_id": data.get("ig_media_id", ""), "fb_post_id": data.get("fb_post_id", ""),
                  "notes": "; ".join(data.get("warnings", []) + data.get("errors", []))}
        crm = self.dex.crm
        try:  # the Sheet log must never block posting
            if data.get("logged"):
                await crm._update("Content", cid, fields)
            else:
                row = {"content_id": cid, "created_at": clock.now(), **fields}
                await crm._run(crm.store.append, "Content", {k: _cell(v) for k, v in row.items()})
                data["logged"] = True
                self._save(cid, kind, status, data)
        except Exception as e:
            log.warning("Content tab log failed: %s", e)

    def _new_id(self) -> str:
        return f"N{self.dex.db.next_counter('nova_content'):04d}"

    # ------------------------------------------------------------------ intake
    async def add_media(self, msg: dict[str, Any]) -> bool:
        """Called by the Telegram webhook for photo/video messages. Returns True if handled."""
        item = None
        if msg.get("photo"):
            item = {"file_id": msg["photo"][-1]["file_id"], "kind": "photo"}  # largest size
        elif msg.get("video"):
            item = {"file_id": msg["video"]["file_id"], "kind": "video",
                    "size": msg["video"].get("file_size", 0)}
        elif (doc := msg.get("document")) and str(doc.get("mime_type", "")).split("/")[0] in ("image", "video"):
            item = {"file_id": doc["file_id"], "kind": "photo" if doc["mime_type"].startswith("image") else "video",
                    "size": doc.get("file_size", 0)}
        if not item:
            return False
        item["note"] = (msg.get("caption") or "").strip()
        item["at"] = clock.now().isoformat()
        if not self.collecting():
            await self.dex.owner.notify("📸 Got it! Keep sending. Nova will prepare Story and post previews about "
                                        "3 minutes after your last photo or video.", urgent=True)
        self.debouncer.add("batch", item)
        return True

    # ------------------------------------------------------------------ processing
    def collecting(self) -> bool:
        """True while Nova is waiting for more photos (in memory, so a restart never leaves it stuck)."""
        return bool(self.debouncer._buffers.get("batch"))

    def add_note(self, text: str) -> None:
        """A plain text message sent while Nova is collecting photos is a note about them, e.g. "Tahoe, Kyle"."""
        self.debouncer.add("batch", {"kind": "note", "note": text.strip(), "at": clock.now().isoformat()})

    async def _process_batch(self, _key: str, items: list[dict[str, Any]]) -> None:
        notes = " ".join(i["note"] for i in items if i.get("note"))
        items = [i for i in items if i["kind"] != "note"]
        loaded: list[dict[str, Any]] = []
        for it in items:
            try:
                if it["kind"] == "video" and int(it.get("size") or 0) > 20 * 1024 * 1024:
                    raise MediaError("FILE_TOO_BIG")
                data = await self.download(it["file_id"])
                frames = [await run_blocking(for_vision, data)] if it["kind"] == "photo" else \
                    [await run_blocking(for_vision, f) for f in await run_blocking(self.frames, data)]
                loaded.append({**it, "data": data, "frames": frames})
            except MediaError as e:
                msg = ("A video was over Telegram's 20 MB limit for bots, so Nova can't use it. Send clips of "
                       "about 30 seconds or less." if str(e) == "FILE_TOO_BIG" else f"Couldn't read a file: {e}")
                await self.dex.owner.notify(f"⚠️ {msg}", urgent=True)
            except Exception as e:
                log.exception("download failed")
                await self.dex.owner.notify(f"⚠️ Couldn't download a file from Telegram ({type(e).__name__}).")
        if not loaded:
            return
        n_p = sum(1 for x in loaded if x["kind"] == "photo")
        n_v = len(loaded) - n_p
        got = " and ".join(f"{n} {k}{'s' if n != 1 else ''}" for n, k in ((n_p, "photo"), (n_v, "video")) if n)
        self.dex.activity.log("Nova", f"Got {got} from you on Telegram", f"Got {len(loaded)} file{'s' * (len(loaded) != 1)} 📸")
        try:
            results = await self.vision.classify([x["frames"] for x in loaded])
        except Exception as e:
            log.exception("vision failed")
            await self.dex.owner.notify(f"⚠️ Nova couldn't look at your photos right now ({type(e).__name__}). "
                                        "Try sending them again in a few minutes.", urgent=True)
            return
        for x, r in zip(loaded, results, strict=True):
            x["r"] = r
        plates = sum(len(x["r"].get("plates") or []) for x in loaded if x["kind"] == "photo")
        houses = sum(len(x["r"].get("house_numbers") or []) for x in loaded if x["kind"] == "photo")
        if plates or houses:
            what = ", ".join(f"{n} {k}{'s' if n != 1 else ''}" for n, k in
                             ((plates, "license plate"), (houses, "house number")) if n)
            self.dex.activity.log("Nova", f"Blurred {what}", f"Blurred {what}")
        await self._make_stories([x for x in loaded if x["r"]["category"] in ("on_the_way", "mid_job")
                                  and x["r"]["quality"] >= MIN_QUALITY])
        await self._make_post([x for x in loaded if x["r"]["category"] in ("before", "after")
                               and x["r"]["quality"] >= MIN_QUALITY and x["kind"] == "photo"], notes)
        skipped = [x for x in loaded if x["r"]["category"] == "skip" or x["r"]["quality"] < MIN_QUALITY]
        if skipped:
            await self.dex.owner.notify(f"Nova skipped {len(skipped)} item(s) that were blurry, duplicates or not "
                                        "usable.")

    @staticmethod
    def _boxes(r: dict[str, Any], keys: tuple[str, ...]) -> list[Box]:
        return [Box(**b) for k in keys for b in (r.get(k) or [])]

    def _warnings(self, x: dict[str, Any], blurred: bool) -> list[str]:
        r, w = x["r"], []
        if x["kind"] == "video":
            if r.get("plates") or r.get("house_numbers"):
                w.append("⚠️ A license plate or house number may be visible in this video (videos can't be blurred)")
            if r.get("faces"):
                w.append("⚠️ A face is visible in this video. Make sure it's only you")
        elif r.get("faces") and not blurred:
            w.append("⚠️ A face is visible. Make sure it's only you")
        if r.get("other_private"):
            w.append(f"⚠️ Possibly private: {r['other_private']}")
        return w

    async def _make_stories(self, xs: list[dict[str, Any]]) -> None:
        for x in xs:
            r = x["r"]
            if x["kind"] == "photo":
                img = await run_blocking(open_image, x["data"])
                img = await run_blocking(blur_boxes, img, self._boxes(r, ("plates", "house_numbers")))
                name = self.host.save(await run_blocking(to_jpeg, await run_blocking(cap_size, img)), "jpg")
            else:
                name = self.host.save(x["data"], "mp4")
            cid = self._new_id()
            data = {"category": r["category"], "media": [name], "media_kind": x["kind"],
                    "warnings": self._warnings(x, blurred=False), "created_at": clock.now().isoformat()}
            self._save(cid, "story", "pending", data)
            await self._log(cid, "story", "pending", data)
            where = "Instagram + Facebook Story" if x["kind"] == "photo" else "Instagram Story (videos can't go to Facebook Stories)"
            label = r["category"].replace("_", " ")
            text = f"{cid} · STORY ({label}) → {where}" + "".join(f"\n{w}" for w in data["warnings"])
            self.dex.activity.log("Nova", f"Sent you a Story preview ({label})", "Story preview sent")
            await self.dex.owner.post_media(x["kind"], [self.host.url(name)], text,
                                            [[("✅ Post Story", f"{cid}:post"), ("❌ Skip", f"{cid}:skip")]])

    async def _make_post(self, xs: list[dict[str, Any]], notes: str) -> None:
        befores = sorted([x for x in xs if x["r"]["category"] == "before"], key=lambda x: -x["r"]["quality"])
        afters = sorted([x for x in xs if x["r"]["category"] == "after"], key=lambda x: -x["r"]["quality"])
        if not afters:
            if befores:
                await self.dex.owner.notify("Nova got before photos but no after photos, so there's no post yet. "
                                            "Send the after shots when you're done!")
            return
        # prefer before/after shots of the same spot, then best quality; befores first, up to 6 photos total
        after_keys = {a["r"].get("pair_key") for a in afters if a["r"].get("pair_key")}
        befores.sort(key=lambda b: (b["r"].get("pair_key") not in after_keys, -b["r"]["quality"]))
        chosen = befores[:3] + afters[: 6 - min(3, len(befores))]
        names, previews = [], []
        for x in chosen:
            img = await run_blocking(open_image, x["data"])
            img = await run_blocking(blur_boxes, img, self._boxes(x["r"], ("plates", "house_numbers", "faces")))
            jpeg = await run_blocking(to_jpeg, await run_blocking(fit_feed, img))
            names.append(self.host.save(jpeg, "jpg"))
            previews.append(await run_blocking(for_vision, jpeg))
        facts = await self._facts(chosen, notes)
        caption, problem = await self._caption(facts, previews)
        cid = self._new_id()
        warnings = [w for x in chosen for w in self._warnings(x, blurred=True)]
        data = {"media": names, "caption": caption, "facts": facts, "warnings": warnings,
                "created_at": clock.now().isoformat()}
        self._save(cid, "post", "pending", data)
        await self._log(cid, "post", "pending", data)
        vehicle = next((x["r"]["vehicle"] for x in chosen if x["r"].get("vehicle")), "")
        self.dex.activity.log("Nova", f"Picked {len(names)} before/after photos{' of the ' + vehicle if vehicle else ''}"
                                      " and wrote the caption", "Before/after post ready")
        await self.dex.owner.post_media("photo", [self.host.url(n) for n in names], f"{cid} · POST preview", None)
        await self._post_card(cid, data, problem)

    async def _post_card(self, cid: str, data: dict[str, Any], problem: str = "") -> None:
        slot = self.next_slot()
        text = (f"{cid} · BEFORE/AFTER POST → Instagram + Facebook\n{len(data['media'])} photos · posts "
                f"{clock.label(slot)}\n\n{data['caption']}"
                + (f"\n\n⚠️ {problem}. Please edit the caption." if problem else "")
                + "".join(f"\n{w}" for w in data.get("warnings", [])))
        await self.dex.owner.post_buttons(text, [[("✅ Post", f"{cid}:post"), ("✏️ Edit caption", f"{cid}:edit")],
                                                 [("❌ Skip", f"{cid}:skip")]])

    async def _facts(self, chosen: list[dict[str, Any]], notes: str) -> str:
        vehicles = sorted({x["r"]["vehicle"] for x in chosen if x["r"].get("vehicle")})
        areas = sorted({x["r"]["area"] for x in chosen if x["r"].get("area") not in (None, "none")})
        lines = [f"Owner's note: {notes}" if notes else "", f"Vehicle seen: {', '.join(vehicles)}" if vehicles else "",
                 f"Area detailed: {', '.join(areas)}" if areas else ""]
        try:  # match today's job for vehicle/service/city (NOVA_SPEC N7)
            today = clock.now().date()
            for j in await self.dex.crm.list_jobs():
                start = clock.parse_iso(str(j.get("start")))
                if start.date() == today and j.get("status") in ("Booked", "Completed"):
                    city = str(j.get("address", "")).split(",")[1].strip() if "," in str(j.get("address", "")) else ""
                    lines.append(f"Today's job: {j.get('service')} detail" + (f" in {city}" if city else ""))
                    break
        except Exception:
            pass
        if not any("in " in ln for ln in lines if ln.startswith("Today")):
            lines.append("City: Kyle, TX area (use Kyle unless the owner's note says otherwise)")
        return "\n".join(ln for ln in lines if ln)

    async def _caption(self, facts: str, previews: list[bytes]) -> tuple[str, str]:
        recent = [c["data"].get("caption", "") for c in self.list(kind="post")[-5:]]
        problem = ""
        for _attempt in range(2):
            try:
                raw = await self.vision.caption(facts + (f"\nFix this problem: {problem}" if problem else ""),
                                                recent, previews)
                return captions.check(raw), ""
            except captions.CaptionRejected as e:
                problem = str(e)
            except Exception as e:
                log.exception("caption failed")
                problem = f"caption writer error ({type(e).__name__})"
                break
        return ("Fresh detail just wrapped up in Kyle! 🚗✨ DM 'DETAIL' for a price on yours.\n"
                "#kyletx #buda #austindetailing #mobiledetailing #cardetailing"), problem

    # ------------------------------------------------------------------ owner decisions
    async def handle_callback(self, cid: str, action: str) -> str:
        c = self.get(cid)
        if not c:
            return f"{cid} not found."
        if c["status"] not in ("pending",):
            return f"{cid} was already {c['status']}."
        data = c["data"]
        if action == "skip":
            self._save(cid, c["kind"], "skipped", data)
            await self._log(cid, c["kind"], "skipped", data)
            return f"{cid} skipped."
        if action == "edit" and c["kind"] == "post":
            self.dex.db.kv_set("telegram_pending_input", {"approval_id": cid, "action": "nova_caption",
                                                          "at": clock.now().isoformat()})
            await self.dex.owner.ask_for_text(f"✏️ {cid}: send the new caption (5 hashtags max, no prices).")
            return ""
        if action == "post" and c["kind"] == "story":
            return await self._publish_story(cid, data)
        if action == "post" and c["kind"] == "post":
            slot = self.next_slot()
            data["scheduled_for"] = slot.isoformat()
            self._save(cid, "post", "scheduled", data)
            await self._log(cid, "post", "scheduled", data)
            self.dex.activity.log("Nova", f"You approved {cid}: it posts {clock.label(slot)}",
                                  f"Posts {clock.label(slot).split(',')[0]} {clock.label(slot).split(' at ')[-1]}")
            return f"✅ {cid} scheduled for {clock.label(slot)} on Instagram + Facebook."
        return "Unknown action."

    async def set_caption(self, cid: str, text: str) -> str:
        c = self.get(cid)
        if not c or c["status"] != "pending":
            return f"{cid} isn't waiting for a caption."
        try:
            c["data"]["caption"] = captions.check(text)
        except captions.CaptionRejected as e:
            self.dex.db.kv_set("telegram_pending_input", {"approval_id": cid, "action": "nova_caption",
                                                          "at": clock.now().isoformat()})
            return f"That caption can't be used ({e}). Send another one."
        self._save(cid, "post", "pending", c["data"])
        await self._post_card(cid, c["data"])
        return ""

    # ------------------------------------------------------------------ scheduling
    def next_slot(self) -> datetime:
        now = clock.now()
        taken = {clock.parse_iso(c["data"]["scheduled_for"]).date() for c in self.list(kind="post")
                 if c["status"] in ("scheduled", "published") and c["data"].get("scheduled_for")}
        day = now.date()
        for _ in range(60):
            if day not in taken:
                slot = datetime(day.year, day.month, day.day, POST_HOUR, POST_MINUTE, tzinfo=clock.TZ)
                if day == now.date():
                    if now.hour >= LAST_POST_HOUR or now + timedelta(minutes=5) > slot.replace(hour=LAST_POST_HOUR):
                        day += timedelta(days=1)
                        continue
                    slot = max(slot, now + timedelta(minutes=5))
                return slot
            day += timedelta(days=1)
        return now + timedelta(days=60)

    async def publish_due(self) -> int:
        n = 0
        for c in self.list(status="scheduled", kind="post"):
            if clock.parse_iso(c["data"]["scheduled_for"]) <= clock.now():
                await self._publish_post(c["content_id"], c["data"])
                n += 1
        return n

    def cleanup_media(self) -> int:
        """Delete media older than 7 days, except anything still waiting for approval or a posting slot."""
        keep = {n for c in self.list() if c["status"] in ("pending", "scheduled") for n in c["data"].get("media", [])}
        return self.host.cleanup(7, keep)

    # ------------------------------------------------------------------ publishing (Composio)
    async def _ig_publish(self, args: dict[str, Any], tool: str = "INSTAGRAM_CREATE_MEDIA_CONTAINER",
                          wait: int = 60) -> str:
        c = self.dex.dm.c
        cont = await c.execute(tool, {"ig_user_id": self.ig_user_id, **args})
        creation = find_id(cont, ("id", "creation_id", "container_id"))
        if not creation:
            raise ComposioError(f"no container id: {cont}")
        pub = await c.execute("INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH",
                              {"ig_user_id": self.ig_user_id, "creation_id": creation, "max_wait_seconds": wait})
        return find_id(pub, ("id", "media_id")) or creation

    async def _publish_story(self, cid: str, data: dict[str, Any]) -> str:
        url = self.host.url(data["media"][0])
        done, errors = [], []
        try:
            args = {"media_type": "STORIES", ("video_url" if data["media_kind"] == "video" else "image_url"): url}
            data["ig_media_id"] = await self._ig_publish(args, wait=180 if data["media_kind"] == "video" else 60)
            done.append("Instagram")
        except ComposioError as e:
            errors.append(f"Instagram: {e}")
        if data["media_kind"] == "photo":
            try:
                up = await self.dex.dm.c.execute("FACEBOOK_UPLOAD_PHOTO", {"page_id": self.page_id, "url": url,
                                                                            "published": False})
                photo_id = find_id(up, ("id", "photo_id"))
                if not photo_id:
                    raise ComposioError(f"no photo id: {up}")
                st = await self.dex.dm.c.execute("FACEBOOK_PUBLISH_PAGE_PHOTO_STORY",
                                                 {"page_id": self.page_id, "photo_id": photo_id})
                data["fb_post_id"] = find_id(st, ("post_id", "id")) or photo_id
                done.append("Facebook")
            except ComposioError as e:
                errors.append(f"Facebook: {e}")
        return await self._finish(cid, "story", data, done, errors)

    async def _publish_post(self, cid: str, data: dict[str, Any]) -> str:
        urls = [self.host.url(n) for n in data["media"]]
        done, errors = [], []
        try:
            if len(urls) == 1:
                data["ig_media_id"] = await self._ig_publish({"image_url": urls[0], "caption": data["caption"]})
            else:
                data["ig_media_id"] = await self._ig_publish({"child_image_urls": urls, "caption": data["caption"]},
                                                             tool="INSTAGRAM_CREATE_CAROUSEL_CONTAINER", wait=120)
            done.append("Instagram")
        except ComposioError as e:
            errors.append(f"Instagram: {e}")
        try:
            fb = await self.dex.dm.c.execute("FACEBOOK_CREATE_MULTI_PHOTO_POST",
                                             {"page_id": self.page_id, "photo_urls": urls, "message": data["caption"]})
            data["fb_post_id"] = find_id(fb, ("id", "post_id"))
            done.append("Facebook")
        except ComposioError as e:
            errors.append(f"Facebook: {e}")
        return await self._finish(cid, "post", data, done, errors)

    async def _finish(self, cid: str, kind: str, data: dict[str, Any], done: list[str], errors: list[str]) -> str:
        data["platforms"], data["errors"] = done, errors
        status = "published" if done else "failed"
        self._save(cid, kind, status, data)
        await self._log(cid, kind, status, data)
        what = "Story" if kind == "story" else "Post"
        where = " + ".join(done)
        self.dex.activity.log("Nova", f"{what} is live on {where}" if done else f"{what} {cid} couldn't be posted",
                              f"{what} is live ✅" if done else f"{what} failed ⚠️")
        if done and not errors:
            msg = f"✅ {cid} {what} is live on {' + '.join(done)}."
        elif done:
            msg = f"⚠️ {cid} {what} posted on {' + '.join(done)}, but failed on: {'; '.join(errors)}"
        else:
            msg = f"❌ {cid} {what} couldn't be posted: {'; '.join(errors)}"
        await self.dex.owner.notify(msg, urgent=True)
        return msg

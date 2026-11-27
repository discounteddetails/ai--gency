"""Pixel: graphic design agent (docs/PIXEL_SPEC.md). Owner asks in Telegram -> 2 options -> pick or change ->
full-size files, and optionally hand them to Nova to post."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app import clock
from app.core import Dex
from app.nova.media import (
    MediaError,
    MediaHost,
    cap_size,
    for_vision,
    open_image,
    run_blocking,
    telegram_download,
    to_jpeg,
    video_frames,
)
from app.pixel.render import FORMATS, RenderError
from app.services.debounce import Debouncer

log = logging.getLogger(__name__)
SCHEMA = "CREATE TABLE IF NOT EXISTS pixel_jobs (job_id TEXT PRIMARY KEY, status TEXT, data_json TEXT, created_at TEXT)"
FORMAT_LABEL = {"feed": "Feed post 4:5", "story": "Story / Reel cover 9:16", "square": "Square 1:1",
                "youtube": "YouTube thumbnail 16:9"}
NOVA_TARGETS = {"feed": ["igf", "fbf"], "square": ["igf", "fbf"], "story": ["igs", "fbs"]}


def is_design_caption(text: str) -> bool:
    return (text or "").strip().lower().startswith(("/design", "/logo"))


class Pixel:
    def __init__(self, dex: Dex, designer, renderer, host: MediaHost, assets_dir: str, *, bot_token: str,
                 nova=None, batch_seconds: float = 6, downloader=None, frame_reader=None):
        self.dex, self.designer, self.renderer, self.host, self.nova = dex, designer, renderer, host, nova
        self.dir = Path(assets_dir)
        (self.dir / "jobs").mkdir(parents=True, exist_ok=True)
        self.download = downloader or (lambda fid: telegram_download(bot_token, fid))
        self.frames = frame_reader or video_frames
        self.debouncer = Debouncer(batch_seconds, self._run_request)
        self._groups: dict[str, str] = {}  # Telegram album id -> "design"/"logo"
        with dex.db._lock:
            dex.db._conn.execute(SCHEMA)

    # ------------------------------------------------------------------ storage
    def _save(self, jid: str, status: str, data: dict[str, Any]) -> None:
        with self.dex.db._lock:
            self.dex.db._conn.execute("INSERT OR REPLACE INTO pixel_jobs VALUES (?,?,?,?)",
                                      (jid, status, json.dumps(data), data.get("created_at", clock.now().isoformat())))

    def get(self, jid: str) -> dict[str, Any] | None:
        rows = self.dex.db._q("SELECT * FROM pixel_jobs WHERE job_id=?", (jid,))
        if not rows:
            return None
        r = dict(rows[0])
        r["data"] = json.loads(r.pop("data_json"))
        return r

    # ------------------------------------------------------------------ taste memory
    def taste(self) -> dict[str, Any]:
        t = self.dex.db.kv_get("pixel_taste") or {}
        return {"rules": t.get("rules", []), "feedback": t.get("feedback", []), "liked": t.get("liked", [])}

    def _store_taste(self, t: dict[str, Any]) -> None:
        self.dex.db.kv_set("pixel_taste", t)

    def remember_rule(self, rule: str) -> str:
        t = self.taste()
        rule = rule.strip().rstrip(".")
        if rule and rule not in t["rules"]:
            t["rules"] = (t["rules"] + [rule])[-15:]
            self._store_taste(t)
        self.dex.activity.log("Pixel", f"Will remember: {rule}", "Noted your style rule 📝")
        return f"📝 Got it. Pixel will always: {rule}"

    def _remember_feedback(self, text: str) -> None:
        t = self.taste()
        t["feedback"] = (t["feedback"] + [text.strip()[:200]])[-15:]
        self._store_taste(t)

    def _remember_liked(self, jid: str, data: dict[str, Any], used: bool = False) -> None:
        opt = data["options"][data.get("picked", 0)]
        page = opt["pages"][0]
        t = self.taste()
        liked = [d for d in t["liked"] if d.get("job") != jid]
        thumb = self.dir / "taste" / f"{jid}.jpg"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        src = self.host.path(page["file"])
        if src:  # keep our own small copy: the posted files are cleaned up after a week
            from PIL import Image
            img = Image.open(src)
            img.thumbnail((640, 640))
            img.convert("RGB").save(thumb, "JPEG", quality=80)
        liked.insert(0, {"job": jid, "kind": data.get("kind"), "label": opt["label"], "format": page["format"],
                         "html": page["html"][:8000], "request": data.get("request", "")[:200], "used": used,
                         "at": clock.now().isoformat()})
        # posted designs first, then newest; keep 8 (sorts are stable)
        liked = sorted(sorted(liked, key=lambda d: d.get("at", ""), reverse=True), key=lambda d: not d.get("used"))[:8]
        t["liked"] = liked
        self._store_taste(t)

    def _taste_for_design(self) -> dict[str, Any]:
        t = self.taste()
        for d in t["liked"][:3]:
            p = self.dir / "taste" / f"{d['job']}.jpg"
            if p.is_file():
                d["jpeg"] = p.read_bytes()
        return t

    def style_text(self) -> str:
        t = self.taste()
        if not (t["rules"] or t["feedback"] or t["liked"]):
            return "Pixel hasn't learned your style yet. Pick designs, ask for changes, or tell her with\n/design remember: <rule>"
        out = ["🎨 What Pixel remembers about your style:"]
        if t["rules"]:
            out += ["", "Rules:"] + [f"• {r}" for r in t["rules"]]
        if t["feedback"]:
            out += ["", "Changes you've asked for:"] + [f"• {f}" for f in t["feedback"][-8:]]
        if t["liked"]:
            out += ["", "Designs you liked:"] + [f"• {d['label']} ({d['kind']}{', posted' if d.get('used') else ''})"
                                                 for d in t["liked"]]
        out += ["", "/style forget clears it all."]
        return "\n".join(out)

    def forget_style(self) -> str:
        self._store_taste({})
        return "🧽 Pixel forgot your saved style. She'll start fresh (brand colors still apply)."

    @property
    def logo(self) -> Path | None:
        p = self.dir / "logo.png"
        return p if p.is_file() else None

    # ------------------------------------------------------------------ intake
    def wants(self, msg: dict[str, Any]) -> bool:
        """Media for Pixel: caption starts with /design or /logo, or it belongs to an album that did."""
        if is_design_caption(msg.get("caption", "")):
            return True
        return bool(msg.get("media_group_id") and msg["media_group_id"] in self._groups)

    async def add_media(self, msg: dict[str, Any]) -> None:
        cap = (msg.get("caption") or "").strip()
        group = msg.get("media_group_id")
        mode = "logo" if cap.lower().startswith("/logo") else self._groups.get(group, "design") if group else "design"
        if group:
            self._groups[group] = self._groups.get(group, mode)
        if mode == "logo":
            await self._set_logo(msg)
            return
        item = None
        if msg.get("photo"):
            item = {"file_id": msg["photo"][-1]["file_id"], "kind": "photo"}
        elif msg.get("video"):
            item = {"file_id": msg["video"]["file_id"], "kind": "video", "size": msg["video"].get("file_size", 0)}
        elif (doc := msg.get("document")) and str(doc.get("mime_type", "")).startswith(("image/", "video/")):
            item = {"file_id": doc["file_id"], "kind": "photo" if doc["mime_type"].startswith("image") else "video",
                    "size": doc.get("file_size", 0)}
        text = cap[len("/design"):].strip() if cap.lower().startswith("/design") else ""
        if item:
            self.debouncer.add("design", {**item, "text": text})
        elif text:
            self.debouncer.add("design", {"kind": "text", "text": text})

    async def request(self, text: str) -> str | None:
        """/design <what you want> with no photos."""
        text = text.strip()
        if text.lower().startswith("remember"):
            rule = text[len("remember"):].lstrip(" :,-")
            return self.remember_rule(rule) if rule else "Tell Pixel what to remember, e.g. /design remember: big Anton headlines"
        if not text:
            return ("🎨 Tell Pixel what you need, e.g.\n/design Black Friday poster, $20 off any detail, book by "
                    "11/30\nAttach photos (or a video for a thumbnail) with /design ... as the caption.")
        self.debouncer.add("design", {"kind": "text", "text": text})
        return None

    async def _set_logo(self, msg: dict[str, Any]) -> None:
        fid = (msg.get("document") or {}).get("file_id") or (msg.get("photo") or [{}])[-1].get("file_id")
        if not fid:
            return
        try:
            data = await self.download(fid)
            import io

            from PIL import Image
            img = Image.open(io.BytesIO(data))
            img.thumbnail((1200, 1200))
            img.convert("RGBA").save(self.dir / "logo.png")
        except Exception as e:
            await self.dex.owner.notify(f"⚠️ Pixel couldn't read that logo ({type(e).__name__}). Try sending it as a "
                                        "file (📎 → File) so it keeps a transparent background.", urgent=True)
            return
        self.dex.activity.log("Pixel", "Saved your new logo. Every design uses it from now on", "Logo saved ✅")
        await self.dex.owner.notify("✅ Logo saved. Pixel will put it on every design from now on. (Send it as a "
                                    "file, not a photo, to keep a transparent background.)", urgent=True)

    # ------------------------------------------------------------------ design
    async def _run_request(self, _key: str, items: list[dict[str, Any]]) -> None:
        text = " ".join(i["text"] for i in items if i.get("text")).strip()
        media = [i for i in items if i["kind"] in ("photo", "video")]
        jid = f"D{self.dex.db.next_counter('pixel_jobs'):04d}"
        jdir = self.dir / "jobs" / jid
        jdir.mkdir(parents=True, exist_ok=True)
        assets: list[dict[str, Any]] = []
        for n, it in enumerate(media[:6], 1):
            try:
                if it["kind"] == "video" and int(it.get("size") or 0) > 20 * 1024 * 1024:
                    raise MediaError("FILE_TOO_BIG")
                raw = await self.download(it["file_id"])
                if it["kind"] == "video":
                    for k, frame in enumerate(await run_blocking(self.frames, raw), 1):
                        assets.append(await self._asset(jdir, f"video{n}_frame{k}", frame,
                                                        f"frame {k} of the owner's video"))
                else:
                    assets.append(await self._asset(jdir, f"photo{n}", raw, "owner's photo"))
            except MediaError as e:
                await self.dex.owner.notify("⚠️ That video is over Telegram's 20 MB bot limit. Send a shorter clip "
                                            "or a screenshot of the frame you want." if str(e) == "FILE_TOO_BIG"
                                            else f"⚠️ Pixel couldn't read a file: {e}", urgent=True)
            except Exception as e:
                log.exception("pixel download failed")
                await self.dex.owner.notify(f"⚠️ Pixel couldn't download a file ({type(e).__name__}).")
        if not text and not assets:
            return
        if not text:
            text = "Make something eye-catching with these for my page."
        data = {"request": text, "assets": [{k: a[k] for k in ("name", "desc", "path")} for a in assets],
                "round": 0, "created_at": clock.now().isoformat()}
        self._save(jid, "designing", data)
        await self.dex.owner.notify(f"🎨 Pixel is on it ({jid}). Designs take about a minute.", urgent=True)
        self.dex.activity.log("Pixel", f"Started designing: {text[:120]}", "Designing… 🎨")
        await self._design(jid, data, assets)

    async def _asset(self, jdir: Path, name: str, raw: bytes, desc: str) -> dict[str, Any]:
        img = await run_blocking(open_image, raw)
        img = await run_blocking(cap_size, img, 2000)
        path = jdir / f"{name}.jpg"
        path.write_bytes(await run_blocking(to_jpeg, img))
        w, h = img.size
        return {"name": name, "desc": f"{desc}, {w}x{h}", "path": str(path),
                "preview_jpeg": await run_blocking(for_vision, path.read_bytes())}

    def _asset_paths(self, data: dict[str, Any]) -> dict[str, Path]:
        paths = {a["name"]: Path(a["path"]) for a in data["assets"] if Path(a["path"]).is_file()}
        if self.logo:
            paths["logo"] = self.logo
        return paths

    async def _design(self, jid: str, data: dict[str, Any], assets: list[dict[str, Any]] | None = None,
                      previous: dict[str, Any] | None = None, feedback: str = "") -> None:
        if assets is None:
            assets = [{**a, "preview_jpeg": await run_blocking(for_vision, Path(a["path"]).read_bytes())}
                      for a in data["assets"] if Path(a["path"]).is_file()]
        try:
            result = await self.designer.design(data["request"], assets, has_logo=bool(self.logo),
                                                previous=previous, feedback=feedback,
                                                taste=self._taste_for_design())
        except Exception as e:
            log.exception("design failed")
            self._save(jid, "failed", data)
            await self.dex.owner.notify(f"⚠️ Pixel couldn't finish {jid} ({type(e).__name__}). Try again in a "
                                        "minute, or simplify the request.", urgent=True)
            return
        paths = self._asset_paths(data)
        options = []
        for opt in result["options"]:
            pages = []
            for p in opt["pages"]:
                if p["format"] not in FORMATS:
                    continue
                try:
                    jpeg = await self.renderer.render(p["html"], p["format"], paths)
                except (RenderError, Exception) as e:  # one bad page must not sink the job
                    log.warning("render failed for %s %s: %s", jid, p["format"], e)
                    continue
                pages.append({"format": p["format"], "html": p["html"], "file": self.host.save(jpeg, "jpg")})
            if pages:
                options.append({"label": opt["label"], "pages": pages})
        if not options:
            self._save(jid, "failed", data)
            await self.dex.owner.notify(f"⚠️ Pixel's designs for {jid} didn't render. Try again or reword it.",
                                        urgent=True)
            return
        data.update({"kind": result["kind"], "title": result["title"], "options": options,
                     "round": data.get("round", 0) + 1})
        self._save(jid, "choosing", data)
        letters = "AB"
        previews = [self.host.url(o["pages"][0]["file"]) for o in options]
        await self.dex.owner.post_media("photo", previews,
                                        f"{jid} · {result['title']}\n" + "\n".join(
                                            f"{letters[i]}: {o['label']}" for i, o in enumerate(options)), None)
        buttons = [[(f"✅ Use {letters[i]}", f"{jid}:pick{i}") for i in range(len(options))],
                   [("✏️ Change…", f"{jid}:edit"), ("🔁 New ideas", f"{jid}:again")], [("❌ Cancel", f"{jid}:cancel")]]
        fmts = ", ".join(FORMAT_LABEL[p["format"]] for p in options[0]["pages"])
        await self.dex.owner.post_buttons(f"{jid} · Pick a design. Each comes in: {fmts}.", buttons)
        self.dex.activity.log("Pixel", f"Sent you {len(options)} design options for “{result['title']}”",
                              f"{len(options)} designs ready")

    # ------------------------------------------------------------------ owner decisions
    async def handle_callback(self, jid: str, action: str) -> str:
        job = self.get(jid)
        if not job:
            return f"{jid} not found."
        data, status = job["data"], job["status"]
        if action == "cancel":
            self._save(jid, "cancelled", data)
            return f"{jid} cancelled."
        if action.startswith("pick") and status in ("choosing", "picked"):
            i = int(action[4:])
            if i >= len(data.get("options", [])):
                return "That option isn't there anymore."
            data["picked"] = i
            self._save(jid, "picked", data)
            self._remember_liked(jid, data)
            opt = data["options"][i]
            for p in opt["pages"]:
                await self.dex.owner.send_document(self.host.url(p["file"]),
                                                   f"{jid} · {FORMAT_LABEL[p['format']]}",
                                                   f"{jid.lower()}-{p['format']}.jpg")
            buttons = [[("✏️ Change…", f"{jid}:edit"), ("✅ Done", f"{jid}:done")]]
            if self.nova and any(p["format"] in NOVA_TARGETS for p in opt["pages"]):
                buttons.insert(0, [("📤 Post with Nova", f"{jid}:nova")])
            await self.dex.owner.post_buttons(f"{jid} · Full-size files above (tap to save).", buttons)
            self.dex.activity.log("Pixel", f"You picked “{opt['label']}” for {data.get('title', jid)}",
                                  "Design delivered ✅")
            return ""
        if action == "edit" and status in ("choosing", "picked"):
            self.dex.db.kv_set("telegram_pending_input", {"approval_id": jid, "action": "pixel_edit",
                                                          "at": clock.now().isoformat()})
            await self.dex.owner.ask_for_text(f"✏️ {jid}: what should change? e.g. \"bigger price, darker blue, "
                                              "use the second photo\"")
            return ""
        if action == "again" and status in ("choosing", "picked"):
            self._save(jid, "designing", data)
            await self.dex.owner.notify(f"🎨 Pixel is trying new ideas for {jid}…", urgent=True)
            await self._design(jid, data, feedback="Completely different styles from before.",
                               previous=self._previous(data))
            return ""
        if action == "nova" and status == "picked" and self.nova:
            return await self._to_nova(jid, data)
        if action == "done":
            self._save(jid, "done", data)
            return f"✅ {jid} done."
        return f"{jid} is {status}."

    def _previous(self, data: dict[str, Any]) -> dict[str, Any]:
        opt = data["options"][data.get("picked", 0)]
        return {"label": opt["label"], "pages": [{"format": p["format"], "html": p["html"]} for p in opt["pages"]]}

    async def revise(self, jid: str, feedback: str) -> str:
        job = self.get(jid)
        if not job or job["status"] not in ("choosing", "picked"):
            return f"{jid} can't be changed now."
        data = job["data"]
        self._remember_feedback(feedback)
        self._save(jid, "designing", data)
        await self.dex.owner.notify(f"🎨 Updating {jid}…", urgent=True)
        await self._design(jid, data, previous=self._previous(data), feedback=feedback)
        return ""

    async def _to_nova(self, jid: str, data: dict[str, Any]) -> str:
        """Hand the picked design to Nova as an offer post: feed/square image to the feeds, story image to Stories."""
        nova = self.nova
        opt = data["options"][data.get("picked", 0)]
        by = {p["format"]: p["file"] for p in opt["pages"]}
        feed = by.get("feed") or by.get("square")
        story = by.get("story")
        targets = (NOVA_TARGETS["feed"] if feed else []) + (NOVA_TARGETS["story"] if story else [])
        facts = f"Owner's note: {data['request']}\nThis is an OFFER post made from a designed poster."
        offer = data.get("kind") == "poster"
        caption, problem = await nova._caption(facts, [await run_blocking(for_vision,
                                                                          self.host.path(feed or story).read_bytes())],
                                               offer=offer)
        cid = nova._new_id()
        post = {"media": [feed] if feed else [story], "story_media": [story] if story else [], "caption": caption,
                "facts": facts, "offer": offer, "warnings": [], "targets": targets,
                "created_at": clock.now().isoformat(), "from_design": jid}
        self._remember_liked(jid, data, used=True)
        nova._save(cid, "post", "pending", post)
        await nova._log(cid, "post", "pending", post)
        await nova._post_card(cid, post, problem)
        self._save(jid, "done", data)
        return ""

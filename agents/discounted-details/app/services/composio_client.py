"""Composio wrapper (PRD §9.1). Only allow-listed tools can be called (FR-M10): Dex can read conversations and
send DMs, never comment, post or edit profiles."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Protocol

log = logging.getLogger(__name__)

ALLOWED_TOOLS = frozenset({
    "INSTAGRAM_LIST_ALL_CONVERSATIONS", "INSTAGRAM_LIST_ALL_MESSAGES", "INSTAGRAM_GET_CONVERSATION",
    "INSTAGRAM_SEND_TEXT_MESSAGE", "INSTAGRAM_MARK_SEEN",
    "FACEBOOK_GET_PAGE_CONVERSATIONS", "FACEBOOK_GET_CONVERSATION_MESSAGES", "FACEBOOK_SEND_MESSAGE",
    "FACEBOOK_MARK_MESSAGE_SEEN",
    # Nova (publish only; never comments or replies)
    "INSTAGRAM_CREATE_MEDIA_CONTAINER", "INSTAGRAM_CREATE_CAROUSEL_CONTAINER", "INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH",
    "INSTAGRAM_GET_POST_STATUS", "FACEBOOK_UPLOAD_PHOTO", "FACEBOOK_PUBLISH_PAGE_PHOTO_STORY",
    "FACEBOOK_CREATE_MULTI_PHOTO_POST",
    # comment-to-DM promos (read comments only; replies go out as private DMs, never public comments)
    "INSTAGRAM_GET_IG_USER_MEDIA", "INSTAGRAM_GET_IG_MEDIA_COMMENTS", "FACEBOOK_GET_PAGE_POSTS", "FACEBOOK_GET_COMMENTS",
})
# Publishing must never be retried blindly: a timeout after Meta accepted it would double-post.
NO_RETRY = frozenset({"INSTAGRAM_POST_IG_USER_MEDIA_PUBLISH", "FACEBOOK_PUBLISH_PAGE_PHOTO_STORY",
                      "FACEBOOK_CREATE_MULTI_PHOTO_POST"})


class ComposioError(Exception):
    code = "UPSTREAM_ERROR"


class WindowClosedError(ComposioError):
    code = "WINDOW_CLOSED"


class RecipientUnavailableError(ComposioError):
    code = "RECIPIENT_UNAVAILABLE"


class ConnectionExpiredError(ComposioError):
    code = "CONNECTION_EXPIRED"


class RateLimitedError(ComposioError):
    code = "RATE_LIMITED"


class ToolNotAllowedError(Exception):
    pass


def classify_error(message: str) -> type[ComposioError]:
    m = message.lower()
    if re.search(r"2018278|2534022|outside of allowed window|24.?hour|messaging window|human_agent.*window", m):
        return WindowClosedError
    if re.search(r"\(#551\)|551|not available|unavailable|cannot message", m):
        return RecipientUnavailableError
    if re.search(r"\b190\b|expired|revoked|invalid oauth|reconnect|connected account.*(inactive|not found)", m):
        return ConnectionExpiredError
    if re.search(r"\b429\b|rate limit|\(#4\)|\(#17\)|\(#32\)|\(#613\)|too many", m):
        return RateLimitedError
    return ComposioError


class ComposioLike(Protocol):
    async def execute(self, slug: str, arguments: dict[str, Any]) -> dict[str, Any]: ...
    async def proxy(self, toolkit: str, endpoint: str, method: str, body: dict[str, Any]) -> dict[str, Any]: ...


class ComposioClient:
    def __init__(self, api_key: str, user_id: str | None = None, ig_account_id: str | None = None,
                 fb_account_id: str | None = None):
        from composio import Composio

        self._c = Composio(api_key=api_key)
        self.on_call = None  # spending tracker counts tool calls (Composio plans have call quotas)
        self._user_id = user_id
        self._accounts = {"INSTAGRAM": ig_account_id, "FACEBOOK": fb_account_id}

    def _account_kwargs(self, prefix: str) -> dict[str, Any]:
        """Composio needs the user ID, plus the connected account ID when one is pinned."""
        out: dict[str, Any] = {}
        if self._user_id:
            out["user_id"] = self._user_id
        if acct := self._accounts.get(prefix):
            out["connected_account_id"] = acct
        return out

    async def execute(self, slug: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if slug not in ALLOWED_TOOLS:
            raise ToolNotAllowedError(slug)
        kwargs = self._account_kwargs(slug.split("_", 1)[0])
        if self.on_call:
            self.on_call()
        last: Exception | None = None
        tries = 1 if slug in NO_RETRY else 3
        for attempt in range(tries):
            try:
                # Tool versions are pinned by Composio; we skip the 'latest' check to avoid startup failures.
                res = await asyncio.to_thread(self._c.tools.execute, slug, arguments,
                                              dangerously_skip_version_check=True, **kwargs)
            except Exception as e:  # transport / 5xx
                last = e
                cls = classify_error(str(e))
                if cls in (ComposioError, RateLimitedError) and attempt < tries - 1:
                    await asyncio.sleep(30 if cls is RateLimitedError else 2 ** attempt)
                    continue
                raise cls(str(e)) from e
            res = dict(res) if not isinstance(res, dict) else res
            if res.get("successful"):
                return res.get("data") or {}
            err = str(res.get("error") or "unknown Composio error")
            cls = classify_error(err)
            if cls is RateLimitedError and attempt < tries - 1:
                await asyncio.sleep(30)
                continue
            raise cls(err)
        raise ComposioError(str(last))

    async def proxy(self, toolkit: str, endpoint: str, method: str, body: dict[str, Any]) -> dict[str, Any]:
        """Raw Graph call through the connected account (only used for the Instagram Human Agent test/send)."""
        if not re.fullmatch(r"/(?:me|\d+)/messages", endpoint) or method != "POST":
            raise ToolNotAllowedError(f"proxy {method} {endpoint}")
        kwargs = self._account_kwargs(toolkit.upper())
        res = await asyncio.to_thread(self._c.tools.proxy, endpoint=endpoint, method=method, body=body, **kwargs)
        res = res if isinstance(res, dict) else getattr(res, "model_dump", lambda: dict(res))()
        status = int(res.get("status", 200) or 200)
        data = res.get("data") or {}
        if status >= 400 or (isinstance(data, dict) and data.get("error")):
            raise classify_error(str(data))(str(data))
        return data

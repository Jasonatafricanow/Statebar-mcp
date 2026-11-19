"""One-way delivery of daily sleep totals to the health plugin (nutrition-tracker).

statebar owns the evidence; the health plugin only consumes. Each wake date's total
goes out once per distinct value through the plugin's health webhook, keyed by a
content-derived ``sourceEventId`` (so retries are idempotent and a corrected total
replaces the day's value). Unconfigured = nothing leaves the process; rows stay queued
and visible on /v1/sleep.

Env:
  STATEBAR_HEALTH_WEBHOOK_URL    e.g. http://127.0.0.1:3456/api/health-webhook
  STATEBAR_HEALTH_WEBHOOK_TOKEN  the plugin's HEALTH_WEBHOOK_TOKEN
  STATEBAR_HEALTH_SUBJECT        which statebar subject is the plugin's user (default xiyue-user)
  STATEBAR_HEALTH_MEMBER         household memberId, only if the plugin runs in household mode
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 10


def _post_json(url: str, token: str, body: Dict[str, Any], timeout: float = 3.0) -> Dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from None


class HealthSync:
    def __init__(self, url: str = "", token: str = "", subject_id: str = "xiyue-user", member_id: str = "",
                 post: Callable[..., Dict[str, Any]] = _post_json):
        self.url, self.token, self.subject_id, self.member_id, self._post = url, token, subject_id, member_id, post

    @classmethod
    def from_env(cls) -> "HealthSync":
        return cls(url=os.environ.get("STATEBAR_HEALTH_WEBHOOK_URL", "").strip(),
                   token=os.environ.get("STATEBAR_HEALTH_WEBHOOK_TOKEN", "").strip(),
                   subject_id=os.environ.get("STATEBAR_HEALTH_SUBJECT", "xiyue-user").strip() or "xiyue-user",
                   member_id=os.environ.get("STATEBAR_HEALTH_MEMBER", "").strip())

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    def wants(self, subject_id: str) -> bool:
        return subject_id == self.subject_id

    def flush(self, store, limit: int = 10) -> Dict[str, Any]:
        """Deliver queued days (best-effort; failures stay queued for the next flush)."""
        if not self.configured:
            return {"configured": False}
        sent = failed = 0
        for row in store.get_sleep_sync(self.subject_id, pending_only=True, limit=limit):
            if row["attempts"] >= MAX_ATTEMPTS:
                continue
            body = dict(row["payload"], source="statebar")
            if self.member_id:
                body["memberId"] = self.member_id
            try:
                result = self._post(self.url, self.token, body)
                if not result.get("ok", False):
                    raise RuntimeError(str(result.get("error") or result)[:300])
                store.mark_sleep_sync(row["subject_id"], row["wake_date"], row["payload_json"], True)
                sent += 1
            except Exception as exc:  # network, auth, plugin refusal
                store.mark_sleep_sync(row["subject_id"], row["wake_date"], row["payload_json"], False, str(exc))
                logger.warning("health sync %s failed: %s", row["wake_date"], exc)
                failed += 1
        return {"configured": True, "sent": sent, "failed": failed}

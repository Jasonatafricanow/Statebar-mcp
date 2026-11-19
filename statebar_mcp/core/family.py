"""Family members the user talks about (P1B-2b): whose sleep a report is.

The user's own sleep stays on the request's subject. A report about someone else is
recorded on that member's subject only when the item names them (``who``) with an
alias that literally appears in the user's words; an unknown or unconfirmed person is
never written. Members come from ``STATEBAR_FAMILY`` (JSON) or the file named by
``STATEBAR_FAMILY_FILE``:

  {"members": [{"subject_id": "kid", "name": "娃", "aliases": ["娃", "闺女"]}]}

Subject ids use the health plugin's household memberId shape so the two line up.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

SUBJECT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
SELF_WORDS = frozenset({"", "我", "自己", "本人", "user", "self", "me"})


@dataclass(frozen=True)
class Member:
    subject_id: str
    name: str
    aliases: Tuple[str, ...]


def parse_members(config: Dict) -> List[Member]:
    members, seen = [], set()
    for m in (config or {}).get("members") or []:
        sid, name = str(m.get("subject_id", "")), str(m.get("name", "")).strip()
        aliases = tuple(a.strip() for a in (m.get("aliases") or [name]) if isinstance(a, str) and a.strip())
        if not SUBJECT_RE.match(sid) or sid in seen or not name or not aliases:
            raise ValueError(f"invalid family member: {m!r}")
        seen.add(sid)
        members.append(Member(sid, name, aliases))
    return members


def load_members(env=os.environ) -> List[Member]:
    raw = env.get("STATEBAR_FAMILY", "").strip()
    path = env.get("STATEBAR_FAMILY_FILE", "").strip()
    if not raw and path and os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    return parse_members(json.loads(raw)) if raw else []


def resolve(who, text: str, members: List[Member], self_subject: str) -> Tuple[Optional[str], str]:
    """(subject_id, "") or (None, reason). ``who`` is what the host wrote for the item."""
    name = str(who or "").strip()
    if name in SELF_WORDS:
        return self_subject, ""
    for m in members:
        if name == m.subject_id or name == m.name or name in m.aliases:
            if any(a in (text or "") for a in m.aliases):
                return m.subject_id, ""
            return None, f"{m.name} is not mentioned in the user's words"
    return None, f"unknown family member {name[:20]!r}"

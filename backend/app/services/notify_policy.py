"""Decides what an event does: notify, notify urgently, or stay silent.

Alert rules are ordered and the first enabled match wins. With no enabled
rules everything behaves exactly as before rules existed (the existing
per-type settings and cooldowns), so an upgrade changes nothing until the user
opts in.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.security import AlertRule

logger = logging.getLogger(__name__)

# Event types a rule can govern (camera health/system events always follow
# their own settings so a rule can never silence "camera offline" by accident).
RULE_KINDS = ("motion", "motion_update", "motion_summary", "loitering", "line_cross", "arrival", "incident",
              "incident_summary", "tamper", "package")


@dataclass
class Decision:
    action: str = "notify"  # notify | notify_high | silent
    rule_id: int | None = None
    rule_name: str | None = None
    cooldown_s: int | None = None
    webhook_url: str | None = None

    @property
    def silent(self) -> bool:
        return self.action == "silent"

    @property
    def urgent(self) -> bool:
        return self.action == "notify_high"


def _match_list(wanted, have) -> bool:
    if not wanted:
        return True
    return bool(set(wanted) & set(have or []))


def matches(rule, event_type: str, camera_id: int | None, mode: str, payload: dict | None) -> bool:
    payload = payload or {}
    if rule.modes and mode not in rule.modes:
        return False
    if rule.camera_ids and camera_id not in rule.camera_ids:
        return False
    if rule.kinds and event_type not in rule.kinds:
        return False
    # Fail safe: when the AI couldn't check this event, "is it a person?" is
    # unknown, not "no" - a rule like "Away: people are urgent" must still fire.
    # (Only towards alerting: a quiet rule never silences an unchecked event.)
    if rule.objects:
        if payload.get("ai_failed"):
            if rule.action == "silent":
                return False
        elif not _match_list(rule.objects, payload.get("objects")):
            return False
    if not _match_list(rule.zones, payload.get("zones")):
        return False
    if rule.identities:
        ids = payload.get("identities") or []
        cats = set(payload.get("categories") or [])
        # Rules written before "Regular"/"Visitor" merged into "Familiar".
        want = {LEGACY_CATEGORIES.get(w, w) for w in rule.identities}
        # A recognised stranger is still someone you don't know: "unknown"
        # rules apply to them, "anyone recognised" rules don't.
        named = cats - {""}
        untrusted_only = bool(named) and named <= {"stranger", "watchlist"}
        have = (set(ids) | cats
                | ({"unknown"} if payload.get("unknown_faces") or "stranger" in cats else set())
                | ({"known"} if ids and not untrusted_only else set()))
        if not want & have:
            return False
    return True


LEGACY_CATEGORIES = {"staff": "familiar", "visitor": "familiar"}


def decide_with(rules: list, event_type: str, camera_id: int | None, mode: str, payload: dict | None) -> Decision:
    if event_type not in RULE_KINDS:
        return Decision()
    # Someone on the watch list is urgent whatever the rules or mode say.
    if "watchlist" in ((payload or {}).get("categories") or []):
        return Decision("notify_high", None, "watch list")
    if event_type == "tamper":
        # A covered or turned camera is urgent in every mode - only a rule
        # written specifically for tamper alerts can change that.
        for rule in sorted((r for r in rules if r.enabled and r.kinds and "tamper" in r.kinds),
                           key=lambda r: (r.position, r.id or 0)):
            if matches(rule, event_type, camera_id, mode, payload):
                return Decision(rule.action, rule.id, rule.name, rule.cooldown_s, rule.webhook_url)
        return Decision("notify_high", None, "camera tampering")
    for rule in sorted((r for r in rules if r.enabled), key=lambda r: (r.position, r.id or 0)):
        if matches(rule, event_type, camera_id, mode, payload):
            return Decision(rule.action, rule.id, rule.name, rule.cooldown_s, rule.webhook_url)
    return Decision()


async def decide(event) -> Decision:
    try:
        async with AsyncSessionLocal() as db:
            rules = (await db.execute(select(AlertRule))).scalars().all()
        if not any(r.enabled for r in rules):
            return decide_with([], event.type, event.camera_id, "home", event.payload)
        from app.services.security_mode import current_mode

        mode = (await current_mode())["mode"]
        return decide_with(rules, event.type, event.camera_id, mode, event.payload)
    except Exception:
        # A broken rule table must never silence alerts.
        logger.exception("Alert rule evaluation failed - notifying as normal")
        return Decision()


PRESETS = [
    # (name, modes, kinds, objects, action, identities) - offered in the UI, created disabled.
    ("Away: any person or vehicle is urgent", ["away"], ["motion", "motion_update", "loitering", "line_cross"],
     ["person", "car", "truck", "motorcycle"], "notify_high", None),
    ("Night: people are urgent", ["night"], ["motion", "motion_update", "loitering", "line_cross"], ["person"],
     "notify_high", None),
    ("Night: vehicles notify", ["night"], ["motion"], ["car", "truck", "motorcycle"], "notify", None),
    ("Home: unknown faces are urgent", ["home"], ["motion", "motion_update", "loitering"], ["person"], "notify_high",
     ["unknown"]),
    ("Home: household members arriving are quiet", ["home"], ["motion", "arrival"], ["person"], "silent",
     ["household"]),
    ("Home: only loitering and line crossing", ["home"], ["loitering", "line_cross"], None, "notify", None),
    ("Home: everything else is quiet", ["home"], ["motion", "motion_update", "motion_summary"], None, "silent", None),
    ("Disarmed: quiet", ["disarmed"], None, None, "silent", None),
]

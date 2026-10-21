from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.services.notify_policy import decide_with
from app.services.security_mode import last_transition, next_transition, resolve
from app.services import signed_media

UTC = timezone.utc
EVERY_DAY = 127


def sch(at, mode, days=EVERY_DAY, enabled=True):
    return SimpleNamespace(at_hhmm=at, mode=mode, days_mask=days, enabled=enabled)


NIGHT_AND_DAY = [sch("23:00", "night"), sch("07:00", "home")]


def state(mode="home", source="manual", set_at=None, hold=False):
    return SimpleNamespace(mode=mode, source=source, set_at=set_at or datetime(2026, 1, 1, tzinfo=UTC),
                           hold=hold, set_by="me")


def at(day, hh, mm=0):  # 2026-10-19 is a Monday
    return datetime(2026, 10, day, hh, mm, tzinfo=UTC)


def test_schedule_resolves_across_midnight():
    assert last_transition(NIGHT_AND_DAY, at(20, 2))[1] == "night"   # 02:00 Tue -> night since 23:00 Mon
    assert last_transition(NIGHT_AND_DAY, at(20, 12))[1] == "home"
    assert last_transition(NIGHT_AND_DAY, at(20, 23, 30))[1] == "night"


def test_weekday_only_schedule_wraps_the_week():
    weekdays = 0b0011111  # Mon-Fri
    s = [sch("09:00", "away", weekdays), sch("18:00", "home", weekdays)]
    # Sunday noon: last change was Friday 18:00 -> home
    assert last_transition(s, at(25, 12))[1] == "home"
    # Monday 10:00 -> away
    assert last_transition(s, at(26, 10))[1] == "away"
    assert next_transition(s, at(25, 12)) == at(26, 9)


def test_manual_choice_lasts_until_next_scheduled_change():
    st = state("away", set_at=at(20, 12))  # set to Away at noon
    assert resolve(st, NIGHT_AND_DAY, at(20, 15), UTC)[0] == "away"
    assert resolve(st, NIGHT_AND_DAY, at(20, 23, 5), UTC) == ("night", "schedule")


def test_hold_ignores_the_schedule():
    st = state("away", set_at=at(20, 12), hold=True)
    assert resolve(st, NIGHT_AND_DAY, at(21, 2), UTC)[0] == "away"


def test_no_schedule_means_manual_mode_stays():
    st = state("night", set_at=at(1, 12))
    assert resolve(st, [], at(20, 12), UTC)[0] == "night"
    assert resolve(st, [sch("07:00", "home", enabled=False)], at(20, 12), UTC)[0] == "night"


def rule(**kw):
    base = dict(id=1, name="r", position=0, enabled=True, modes=None, camera_ids=None, kinds=None, objects=None,
                zones=None, identities=None, action="notify", cooldown_s=None, webhook_url=None)
    base.update(kw)
    return SimpleNamespace(**base)


def test_no_rules_or_disabled_rules_keep_default_behaviour():
    assert decide_with([], "motion", 1, "away", {}).action == "notify"
    assert decide_with([rule(enabled=False, action="silent")], "motion", 1, "away", {}).action == "notify"


def test_first_matching_rule_wins_in_order():
    rules = [
        rule(id=2, position=20, modes=["home"], action="silent"),
        rule(id=1, position=10, modes=["home"], objects=["person"], zones=["Doorstep"], action="notify_high"),
    ]
    d = decide_with(rules, "loitering", 1, "home", {"objects": ["person"], "zones": ["Doorstep"]})
    assert d.action == "notify_high" and d.rule_id == 1
    d2 = decide_with(rules, "motion", 1, "home", {"objects": ["car"]})
    assert d2.action == "silent"


def test_rules_never_touch_camera_health_events():
    rules = [rule(action="silent")]
    assert decide_with(rules, "camera_offline", 1, "disarmed", {}).action == "notify"
    assert decide_with(rules, "system", None, "disarmed", {}).action == "notify"


def test_camera_and_mode_filters():
    rules = [rule(modes=["away"], camera_ids=[3], action="notify_high")]
    assert decide_with(rules, "motion", 3, "away", {}).action == "notify_high"
    assert decide_with(rules, "motion", 4, "away", {}).action == "notify"
    assert decide_with(rules, "motion", 3, "home", {}).action == "notify"


def test_identity_filters_known_and_unknown():
    rules = [rule(identities=["unknown"], action="notify_high")]
    assert decide_with(rules, "motion", 1, "home", {"unknown_faces": True}).action == "notify_high"
    assert decide_with(rules, "motion", 1, "home", {"identities": ["person:3"]}).action == "notify"
    rules = [rule(identities=["person:3"], action="silent")]
    assert decide_with(rules, "arrival", 1, "home", {"identities": ["person:3"]}).action == "silent"


def test_signed_snapshot_links_verify_and_expire(monkeypatch):
    import asyncio

    monkeypatch.setattr(signed_media.settings, "jwt_secret_key", "secret-for-test")
    path = asyncio.run(signed_media.signed_event_snapshot_path(42, ttl=60))
    q = dict(part.split("=") for part in path.split("?")[1].split("&"))
    assert signed_media.verify(42, int(q["exp"]), q["sig"])
    assert not signed_media.verify(43, int(q["exp"]), q["sig"])  # another event
    assert not signed_media.verify(42, int(q["exp"]) - 3600, q["sig"])  # tampered expiry
    assert not signed_media.verify(42, int((datetime.now(UTC) - timedelta(seconds=5)).timestamp()),
                                   signed_media._sig(42, int((datetime.now(UTC) - timedelta(seconds=5)).timestamp())))


def test_watchlist_is_always_urgent_and_household_can_be_quiet_at_home():
    silent_all = [rule(action="silent")]
    d = decide_with(silent_all, "arrival", 1, "home", {"identities": ["person:9"], "categories": ["watchlist"]})
    assert d.action == "notify_high"
    rules = [rule(modes=["home"], identities=["household"], action="silent")]
    assert decide_with(rules, "arrival", 1, "home", {"identities": ["person:1"], "categories": ["household"]}).action == "silent"
    assert decide_with(rules, "arrival", 1, "away", {"identities": ["person:1"], "categories": ["household"]}).action == "notify"


def test_tamper_is_urgent_even_when_disarmed():
    rules = [rule(modes=["disarmed"], action="silent")]
    assert decide_with(rules, "tamper", 1, "disarmed", {"tamper": "dark"}).action == "notify_high"
    explicit = [rule(kinds=["tamper"], camera_ids=[1], action="notify")]
    assert decide_with(explicit, "tamper", 1, "away", {}).action == "notify"


def test_presets_are_well_formed_and_household_preset_is_filtered():
    from app.services.notify_policy import PRESETS

    for p in PRESETS:
        assert len(p) == 6
    household = [p for p in PRESETS if "household" in p[0]]
    assert household and household[0][5] == ["household"]


def test_rules_fail_safe_when_ai_could_not_check():
    rules = [rule(modes=["away"], objects=["person"], action="notify_high")]
    assert decide_with(rules, "motion", 1, "away", {"objects": []}).action == "notify"
    assert decide_with(rules, "motion", 1, "away", {"objects": [], "ai_failed": True}).action == "notify_high"

    quiet = [rule(modes=["home"], objects=["car"], action="silent")]
    assert decide_with(quiet, "motion", 1, "home", {"objects": [], "ai_failed": True}).action == "notify"

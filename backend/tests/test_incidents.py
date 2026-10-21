import asyncio

from app.services import incidents as inc_mod
from app.services.incidents import IncidentManager, Link, area_rank


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def manager(links=None):
    clock = Clock()
    sent = []

    async def emit(kind, message, snapshot, payload, camera_id, inc):
        sent.append((kind, message, payload))

    m = IncidentManager(emit=emit, clock=clock)
    m.set_links(links or [])
    return m, clock, sent


def run(coro):
    return asyncio.run(coro)


def test_no_areas_configured_cameras_close_in_time_are_one_visit():
    async def go():
        m, clock, sent = manager()
        a, ma, alert_a = await m.join(1, "Gate", None)
        m.mark_alerted(a)  # the worker does this once the first alert went out
        clock.t += 20
        b, mb, alert_b = await m.join(2, "Porch", None)
        return a is b, alert_a, alert_b

    same, alert_a, alert_b = run(go())
    assert same and alert_a and not alert_b  # one alert for the visit


def test_unlinked_areas_far_apart_are_separate_visits():
    async def go():
        m, clock, _ = manager()
        a, ma, _ = await m.join(1, "Gate", "Front")
        await m.leave(a, ma, {"person": 1}, [], None, None, [])
        clock.t += 100
        b, mb, alert_b = await m.join(2, "Garden", "Back")
        return a is b, alert_b

    same, alert_b = run(go())
    assert not same and alert_b


def test_linked_areas_within_walking_time_join():
    links = [Link("Gate", "Driveway", 5, 40), Link("Driveway", "Kitchen Door", 5, 30)]

    async def go():
        m, clock, sent = manager(links)
        a, ma, _ = await m.join(1, "Gate cam", "Gate")
        m.mark_alerted(a)
        clock.t += 6
        await m.leave(a, ma, {"person": 1}, [], (990, 1010), b"x", [])
        clock.t += 15
        b, mb, _ = await m.join(2, "Drive cam", "Driveway")
        clock.t += 10
        await m.leave(b, mb, {"person": 1}, [], (1015, 1035), None, [])
        clock.t += 20
        c, mc, alert_c = await m.join(3, "Door cam", "Kitchen Door")
        clock.t += 300
        await m.leave(c, mc, {"person": 1}, [("person #1", 290, "Kitchen Door")], (1050, 1360), None, [])
        clock.t += inc_mod.IDLE_CLOSE + 1
        closed = await m.sweep()
        return a is b is c, closed, sent, alert_c

    same, closed, sent, alert_c = run(go())
    assert same and len(closed) == 1
    assert not alert_c  # the front door is an expected part of a visit: no extra alert
    kind, message, payload = sent[0]
    assert kind == "incident_summary"
    assert "Route: Gate -> Driveway -> Kitchen Door" in message
    assert "Seen: 1 person" in message  # not 3 - the same person on 3 cameras
    assert "waited ~5m at Kitchen Door" in message
    assert payload["areas"] == ["Gate", "Driveway", "Kitchen Door"]


def test_linked_but_too_late_is_a_new_visit():
    links = [Link("Gate", "Driveway", 5, 40)]

    async def go():
        m, clock, _ = manager(links)
        a, ma, _ = await m.join(1, "Gate cam", "Gate")
        await m.leave(a, ma, {}, [], None, None, [])
        clock.t += 200
        b, _, _ = await m.join(2, "Drive cam", "Driveway")
        return a is b

    assert run(go()) is False


def test_single_camera_visit_keeps_its_own_summary_and_no_visit_summary():
    async def go():
        m, clock, sent = manager()
        a, ma, _ = await m.join(1, "Gate", None)
        own = await m.leave(a, ma, {"person": 1}, [], None, None, [])
        clock.t += inc_mod.IDLE_CLOSE + 1
        await m.sweep()
        return own, sent

    own, sent = run(go())
    assert own is True and sent == []


def test_open_incident_not_closed_while_a_camera_is_still_active():
    async def go():
        m, clock, _ = manager()
        a, ma, _ = await m.join(1, "Gate", None)
        clock.t += 5
        b, mb, _ = await m.join(2, "Porch", None)
        await m.leave(a, ma, {}, [], None, None, [])
        clock.t += 500
        closed = await m.sweep()
        return closed, a.active

    closed, active = run(go())
    assert closed == [] and active


def test_area_rank_orders_public_to_private():
    assert area_rank("Street") < area_rank("Front Gate") < area_rank("Driveway") < area_rank("Back door") \
        < area_rank("Living room interior")
    assert area_rank("Somewhere") == -1


def test_reaching_a_private_area_alerts_again():
    links = [Link("Gate", "Backyard", 5, 60)]

    async def go():
        m, clock, _ = manager(links)
        a, ma, _ = await m.join(1, "Gate cam", "Gate")
        m.mark_alerted(a)
        clock.t += 20
        b, mb, alert_b = await m.join(2, "Back cam", "Backyard")
        return a is b, alert_b

    same, alert_b = run(go())
    assert same and alert_b


def test_visit_whose_camera_never_reported_its_end_is_closed_eventually():
    async def go():
        m, clock, _ = manager()
        a, ma, _ = await m.join(1, "Gate", None)  # never leaves (worker killed mid-event)
        clock.t += inc_mod.MAX_INCIDENT + inc_mod.IDLE_CLOSE + 1
        return await m.sweep(), m.open

    closed, still_open = run(go())
    assert len(closed) == 1 and still_open == []

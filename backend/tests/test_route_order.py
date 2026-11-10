"""The live page's fixed API paths reach their own routes - not cameras'
/api/cameras/{camera_id}, which took "stream-stats" for a camera id (422)."""

from starlette.routing import Match


def test_the_live_pages_fixed_paths_are_not_taken_for_a_camera_id():
    from app.main import app

    def first_route(path):
        scope = {"type": "http", "path": path, "method": "GET", "root_path": ""}
        return next(r for r in app.routes if r.matches(scope)[0] == Match.FULL)

    for path in ("/api/cameras/stream-stats", "/api/cameras/motion-status"):
        assert "{camera_id}" not in first_route(path).path, path
    assert first_route("/api/cameras/5").path == "/api/cameras/{camera_id}"

import json
import urllib.error
import urllib.request

import pytest

from puntoriesgo.app_context import AppContext
from puntoriesgo.models import RiskType, Severity


@pytest.fixture
def ctx(cfg):
    c = AppContext(cfg)
    c.set_online(False)  # sin red: tiles sólo desde caché
    c.start()
    yield c
    c.stop()


def get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.headers.get("Content-Type"), r.read()


def test_routes(ctx):
    base = ctx.map_url
    st, ct, body = get(base)
    assert st == 200 and b"leaflet.js" in body
    st, ct, body = get(base + "static/vendor/leaflet/leaflet.js")
    assert st == 200 and "javascript" in ct
    cfgj = json.loads(get(base + "api/config")[2])
    assert cfgj["bounds"] and cfgj["catalog"]["severities"]
    layers = json.loads(get(base + "api/layers")[2])
    assert len(layers["features"]) == 11
    st, ct, body = get(base + "tiles/17/1000/2000")
    assert ct == "image/png"  # tile vacío offline


def test_token_and_traversal(ctx):
    root = f"http://127.0.0.1:{ctx.server.port}/"
    with pytest.raises(urllib.error.HTTPError) as e:
        get(root + "api/config")
    assert e.value.code == 404
    with pytest.raises(urllib.error.HTTPError) as e:
        get(ctx.map_url + "static/../../puntoriesgo/config.py")
    assert e.value.code in (403, 404)


def test_create_alert_and_events(ctx):
    events = []
    ctx.add_event_listener(events.append)
    a = ctx.create_alert(-34.2050 + 0.5 * 0.0045, -70.78 + 0.5 * 0.0055,
                         RiskType.HOYO, Severity.CRITICA, accuracy_m=4)
    assert a.sector_id == "S02" and a.equipment_id == "E03"
    alerts = json.loads(get(ctx.map_url + "api/alerts")[2])
    assert alerts["features"][0]["properties"]["sector_name"].startswith("Sector 2")
    state = json.loads(get(ctx.map_url + "api/state")[2])
    assert state["alerts_version"] >= 1

    req = urllib.request.Request(ctx.map_url + "api/events", method="POST",
                                 data=json.dumps({"type": "map_pick", "lat": 1, "lon": 2}).encode(),
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=5).read()
    assert events == [{"type": "map_pick", "lat": 1, "lon": 2}]


def test_photo_compression(ctx, tmp_path):
    from PIL import Image

    big = tmp_path / "big.jpg"
    Image.new("RGB", (4000, 3000), (120, 80, 40)).save(big, quality=95)
    a = ctx.create_alert(-34.2, -70.78, RiskType.PIEDRA, Severity.BAJA, photo_path=str(big))
    with Image.open(a.photo_path) as img:
        assert max(img.size) == ctx.cfg.photo_max_side_px
    st, ct, body = get(ctx.map_url + f"photos/{a.id}.jpg")
    assert st == 200 and ct == "image/jpeg"

import io
import threading

import pytest

from puntoriesgo.config import TILE_SOURCES
from puntoriesgo.geo.tile_cache import (
    MBTilesStore, TileDownloader, TileFetcher, TileLimitExceeded, TileProvider,
)
from puntoriesgo.geo.tile_math import BBox, count_tiles, lonlat_to_tile, tile_bbox

SRC = TILE_SOURCES["esri_world_imagery"]


def jpeg(color=(10, 120, 30)) -> bytes:
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (256, 256), color).save(out, format="JPEG")
    return out.getvalue()


class FakeOpener:
    def __init__(self, fail_every=0):
        self.calls = 0
        self.fail_every = fail_every
        self.lock = threading.Lock()

    def __call__(self, req, timeout):
        with self.lock:
            self.calls += 1
            n = self.calls
        if self.fail_every and n % self.fail_every == 0:
            raise OSError("red caída")
        return jpeg()


def test_tile_math_roundtrip():
    x, y = lonlat_to_tile(-70.78, -34.2, 17)
    bb = tile_bbox(x, y, 17)
    assert bb.contains_point(-70.78, -34.2)
    area = BBox(-70.79, -34.21, -70.77, -34.19)
    assert count_tiles(area, 15, 15) >= 1


def test_store_roundtrip(tmp_path):
    st = MBTilesStore(tmp_path / "t.mbtiles", SRC)
    st.put(15, 10, 20, b"abc")
    assert st.get(15, 10, 20) == b"abc"
    assert st.has(15, 10, 20) and not st.has(15, 10, 21)
    assert st.existing(15) == {(10, 20)}
    assert st.stats()["count"] == 1
    assert st.get_metadata()["scheme"] == "tms"


def test_plan_and_download_resumable(tmp_path):
    st = MBTilesStore(tmp_path / "t.mbtiles", SRC)
    opener = FakeOpener()
    dl = TileDownloader(st, TileFetcher(SRC, opener=opener, retries=1), workers=4)
    areas = [BBox(-70.785, -34.205, -70.775, -34.195)]
    plan = dl.plan(areas, 14, 16)
    assert plan.to_download == count_tiles(areas[0], 14, 16)
    seen = []
    prog = dl.run(plan, on_progress=seen.append)
    assert prog.finished and prog.done == plan.to_download and prog.failed == 0
    assert st.stats()["count"] == plan.to_download
    assert seen  # hubo reportes de progreso
    # Reanudar: nada pendiente
    plan2 = dl.plan(areas, 14, 16)
    assert plan2.to_download == 0 and plan2.already_cached == plan.to_download


def test_plan_limit(tmp_path):
    st = MBTilesStore(tmp_path / "t.mbtiles", SRC)
    dl = TileDownloader(st, TileFetcher(SRC, opener=FakeOpener()))
    with pytest.raises(TileLimitExceeded):
        dl.plan([BBox(-71, -35, -70, -34)], 10, 18, max_tiles=1000)


def test_download_cancel(tmp_path):
    st = MBTilesStore(tmp_path / "t.mbtiles", SRC)
    dl = TileDownloader(st, TileFetcher(SRC, opener=FakeOpener()), workers=2)
    plan = dl.plan([BBox(-70.8, -34.22, -70.76, -34.18)], 15, 17)
    cancel = threading.Event()
    cancel.set()
    prog = dl.run(plan, cancel=cancel)
    assert prog.cancelled and prog.done < plan.to_download


def test_provider_offline_overzoom(tmp_path):
    st = MBTilesStore(tmp_path / "t.mbtiles", SRC)
    x, y = lonlat_to_tile(-70.78, -34.2, 16)
    st.put(16, x, y, jpeg((200, 0, 0)))
    online = {"v": False}
    prov = TileProvider(st, TileFetcher(SRC, opener=FakeOpener()), is_online=lambda: online["v"])
    assert prov.get_tile(16, x, y)[1] == "cache"
    data, origin = prov.get_tile(18, x * 4 + 1, y * 4 + 2)  # hijo a 2 niveles
    assert origin == "overzoom" and data[:2] == b"\xff\xd8"
    assert prov.get_tile(16, x + 5, y)[1] == "miss"
    online["v"] = True
    assert prov.get_tile(16, x + 5, y)[1] == "network"
    assert st.has(16, x + 5, y)  # cache on browse


def test_download_aborts_when_network_lost(tmp_path):
    class DeadOpener:
        def __call__(self, req, timeout):
            raise OSError("sin señal")

    st = MBTilesStore(tmp_path / "t.mbtiles", SRC)
    fetcher = TileFetcher(SRC, opener=DeadOpener(), retries=1)
    dl = TileDownloader(st, fetcher, workers=2, max_consecutive_failures=5)
    plan = dl.plan([BBox(-70.8, -34.22, -70.76, -34.18)], 15, 17)
    prog = dl.run(plan)
    assert prog.aborted_reason and not prog.cancelled
    assert prog.failed < plan.to_download

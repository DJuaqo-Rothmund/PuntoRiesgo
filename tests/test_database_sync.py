from pathlib import Path

from puntoriesgo.data.database import Database
from puntoriesgo.data.sync import SyncBackend, SyncError, SyncWorker
from puntoriesgo.models import Alert, RiskType, Severity, SyncState


class FakeBackend(SyncBackend):
    def __init__(self):
        self.reachable = False
        self.fail_next = False
        self.alerts = {}
        self.photos = []

    def is_reachable(self):
        return self.reachable

    def upload_photo(self, alert, path: Path):
        self.photos.append(alert.id)
        return f"https://cdn/{alert.id}.jpg"

    def upsert_alert(self, alert):
        if self.fail_next:
            self.fail_next = False
            raise SyncError("timeout")
        self.alerts[alert.id] = alert.to_remote_payload()


def mk(db, **kw):
    a = Alert(lat=-34.2, lon=-70.78, risk_type=RiskType.ZANJA, severity=Severity.ALTA, **kw)
    return db.create_alert(a)


def test_offline_queue_then_flush(tmp_path):
    db = Database(tmp_path / "x.sqlite3")
    be = FakeBackend()
    w = SyncWorker(db, be)
    photo = tmp_path / "p.jpg"
    photo.write_bytes(b"jpg")
    a1 = mk(db, photo_path=str(photo))
    a2 = mk(db)
    assert db.pending_count() == 2

    # Sin red: no se sincroniza nada, la cola se conserva
    assert w.run_once() == 0
    assert db.pending_count() == 2

    # Vuelve la red, pero el primer upsert falla -> backoff
    be.reachable = True
    be.fail_next = True
    w.run_once()
    assert db.pending_count() == 1
    assert db.get_alert(a1.id).remote_photo_url  # la foto quedó subida y persistida

    # Al reconectar se resetea el backoff y se vacía la cola
    w.set_online(False)
    w.set_online(True)
    w.run_once()
    assert db.pending_count() == 0
    assert set(be.alerts) == {a1.id, a2.id}
    assert be.photos == [a1.id]  # la foto no se volvió a subir
    assert db.get_alert(a2.id).sync_state == SyncState.SYNCED
    assert "photo_path" not in be.alerts[a1.id]


def test_geojson_export(tmp_path):
    db = Database(tmp_path / "x.sqlite3")
    mk(db, description="rama")
    gj = db.alerts_geojson()
    f = gj["features"][0]
    assert f["geometry"]["coordinates"] == [-70.78, -34.2]
    assert f["properties"]["color"] == Severity.ALTA.color

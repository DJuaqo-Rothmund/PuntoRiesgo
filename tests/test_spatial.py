import pytest

from puntoriesgo.geo.spatial import SpatialIndex, haversine_m, point_in_ring, shapely_available
from puntoriesgo.geo.vector_layers import load_vector_file

ENGINES = ["pure"] + (["shapely"] if shapely_available() else [])

LAT0, LON0, DLAT, DLON = -34.2050, -70.7800, 0.0045, 0.0055


def pt(i, j):
    return LAT0 + i * DLAT, LON0 + j * DLON


def test_point_in_ring_basic():
    sq = [(0, 0), (10, 0), (10, 10), (0, 10), (0, 0)]
    assert point_in_ring(5, 5, sq)
    assert point_in_ring(10, 5, sq)  # borde
    assert not point_in_ring(11, 5, sq)


def test_haversine():
    # 1 grado de latitud ≈ 111.2 km
    assert haversine_m(0, 0, 1, 0) == pytest.approx(111_195, rel=1e-3)


@pytest.mark.parametrize("engine", ENGINES)
def test_locate_inside_sector(sample_path, engine):
    idx = SpatialIndex(load_vector_file(sample_path), engine=engine)
    lat, lon = pt(0.5, 0.5)
    m = idx.locate(lat, lon)
    assert m.sector_id == "S02" and m.sector_inside and m.sector_distance_m == 0
    # equipo más cercano global y dentro del mismo sector
    assert m.equipment_in_sector_id == "E03"
    assert m.equipment_distance_m is not None


@pytest.mark.parametrize("engine", ENGINES)
def test_hole_is_outside_but_within_tolerance(sample_path, engine):
    idx = SpatialIndex(load_vector_file(sample_path), engine=engine, sector_tolerance_m=200)
    lat, lon = pt(1.5, 1.5)  # centro del tranque (hueco del sector 4)
    m = idx.locate(lat, lon)
    assert m.sector_id == "S04"
    assert not m.sector_inside
    assert 50 < m.sector_distance_m < 80  # hueco de ±0.0006° ≈ 55-67 m


@pytest.mark.parametrize("engine", ENGINES)
def test_far_outside(sample_path, engine):
    idx = SpatialIndex(load_vector_file(sample_path), engine=engine, sector_tolerance_m=25)
    m = idx.locate(-34.3, -70.9)
    assert m.sector_id is None
    assert m.sector_label == "Fuera de sectores"
    assert m.equipment_id is not None  # siempre hay un equipo más cercano


def test_engines_agree(sample_path):
    if not shapely_available():
        pytest.skip("Shapely no instalado")
    layers = load_vector_file(sample_path)
    a = SpatialIndex(layers, engine="pure", sector_tolerance_m=40)
    b = SpatialIndex(layers, engine="shapely", sector_tolerance_m=40)
    for i in range(-2, 25):
        for j in range(-2, 25):
            lat, lon = LAT0 + i * DLAT / 10, LON0 + j * DLON / 10
            ma, mb = a.locate(lat, lon), b.locate(lat, lon)
            assert (ma.sector_id, ma.sector_inside, ma.equipment_id) == (
                mb.sector_id, mb.sector_inside, mb.equipment_id), (lat, lon)
            if ma.sector_distance_m is not None:
                assert ma.sector_distance_m == pytest.approx(mb.sector_distance_m, abs=0.2)


def test_real_farm_layer_equipment_from_sector():
    from conftest import REAL

    layers = load_vector_file(REAL)
    assert len(layers.sectors) == 40 and not layers.equipment
    idx = SpatialIndex(layers, engine="pure")
    m = idx.locate(-39.553952, -72.478537)  # centroide declarado de E2-S10
    assert m.sector_id == "E2-S10" and m.sector_inside
    assert m.equipment_name == "Equipo 2" and m.equipment_distance_m is None
    assert m.equipment_label == "Equipo 2 (equipo del sector)"

import zipfile

from puntoriesgo.geo.vector_layers import load_vector_file

KML = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
<Document><name>Fundo</name>
  <Folder><name>Sectores</name>
    <Placemark><name>Cuartel A</name>
      <ExtendedData><Data name="cultivo"><value>Nogal</value></Data></ExtendedData>
      <Polygon><outerBoundaryIs><LinearRing><coordinates>
        -70.0,-34.0,0 -69.99,-34.0,0 -69.99,-33.99,0 -70.0,-33.99,0
      </coordinates></LinearRing></outerBoundaryIs></Polygon>
    </Placemark>
  </Folder>
  <Folder><name>Equipos de riego</name>
    <Placemark><name>Valvula 7</name>
      <ExtendedData><SchemaData><SimpleData name="codigo">V7</SimpleData></SchemaData></ExtendedData>
      <Point><coordinates>-69.995,-33.995,0</coordinates></Point>
    </Placemark>
    <Placemark><name>Matriz</name>
      <LineString><coordinates>-70.0,-34.0 -69.99,-33.99</coordinates></LineString>
    </Placemark>
  </Folder>
</Document></kml>"""


def test_geojson_classification(sample_path):
    layers = load_vector_file(sample_path)
    assert len(layers.sectors) == 4
    assert len(layers.equipment) == 7
    s4 = next(s for s in layers.sectors if s.id == "S04")
    assert s4.name == "Sector 4 - Uva de Mesa"
    assert len(s4.geometry["coordinates"]) == 2  # anillo exterior + hueco
    assert layers.extent() is not None
    assert len(layers.work_areas(100)) >= 4


def test_kml_and_kmz(tmp_path):
    p = tmp_path / "fundo.kml"
    p.write_bytes(KML)
    layers = load_vector_file(p)
    assert [s.name for s in layers.sectors] == ["Cuartel A"]
    assert layers.sectors[0].properties["cultivo"] == "Nogal"
    assert layers.sectors[0].geometry["coordinates"][0][0] == layers.sectors[0].geometry["coordinates"][0][-1]
    assert layers.equipment[0].id == "V7"
    assert layers.equipment[0].geometry == {"type": "Point", "coordinates": [-69.995, -33.995]}
    assert len(layers.other) == 1

    kmz = tmp_path / "fundo.kmz"
    with zipfile.ZipFile(kmz, "w") as zf:
        zf.writestr("doc.kml", KML)
    assert len(load_vector_file(kmz).sectors) == 1

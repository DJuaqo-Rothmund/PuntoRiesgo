# PuntoRiesgo

App móvil Offline-First para controlar riesgos en terreno (hoyos, zanjas, piedras, ramas caídas, caminos inundados, etc.) sobre un mapa satelital de alta resolución. Hace un cruce espacial automático con los sectores y los equipos de riego del predio.

| Pieza | Tecnología |
|---|---|
| UI multiplataforma (Android/iOS) | **Flet 1.0** (Python → Flutter) |
| Mapa | **Leaflet 1.9.4** empaquetado localmente, dentro de `flet-webview` |
| Mapa base | ESRI World Imagery (por defecto) o Mapbox Satellite |
| Caché offline de tiles | **MBTiles** (SQLite estándar) + servidor local 127.0.0.1 |
| Datos locales | **SQLite** + Sync Queue (patrón Outbox) |
| Sincronización | Worker en segundo plano → **Supabase** (REST, sin SDK); se puede cambiar por otro backend |
| Cruce espacial | **Shapely 2 / STRtree** si está disponible; si no, un motor en Python puro (mismo resultado) |
| GPS | `flet-geolocator` |

---

## 1. Estructura del proyecto

```
PuntoRiesgo/
├── pyproject.toml              # Config de `flet build` (permisos, dependencias móviles)
├── requirements.txt            # Dependencias de desarrollo/escritorio
├── scripts/
│   └── precache_tiles.py       # CLI: pre-descarga de tiles a .mbtiles desde un PC
├── src/
│   ├── main.py                 # Punto de entrada (ft.run)
│   ├── assets/
│   │   ├── data/sectores_riego.geojson   # Capa de ejemplo: 4 sectores + 7 equipos
│   │   └── web/
│   │       ├── map.html / map.css / map.js   # Mapa Leaflet (pines, capas, GPS, long-press)
│   │       └── vendor/leaflet/               # Leaflet local (funciona sin internet)
│   └── puntoriesgo/
│       ├── config.py           # AppConfig + fuentes de tiles (ESRI/Mapbox)
│       ├── models.py           # RiskType, Severity (colores), Alert
│       ├── app_context.py      # Composición de servicios (núcleo sin UI, testeable)
│       ├── geo/
│       │   ├── tile_math.py    # Matemática XYZ/Web Mercator, BBox
│       │   ├── tile_cache.py   # MBTilesStore, TileFetcher, TileDownloader, TileProvider
│       │   ├── vector_layers.py# Lector GeoJSON / KML / KMZ → sectores y equipos
│       │   └── spatial.py      # SpatialIndex: sector + equipo de riego más cercano
│       ├── data/
│       │   ├── database.py     # SQLite: alerts + sync_queue (transacción atómica)
│       │   ├── photos.py       # Compresión de fotos (1600 px, JPEG 70, sin EXIF)
│       │   └── sync.py         # SyncWorker + SupabaseBackend (backoff exponencial)
│       ├── server/
│       │   └── local_server.py # HTTP 127.0.0.1 con token: tiles, API JSON, fotos
│       └── ui/
│           ├── app.py          # Pantalla principal, GPS, conectividad, FAB
│           ├── map_view.py     # WebView (o alternativa en escritorio)
│           ├── alert_form.py   # Formulario "Registrar Alerta"
│           └── offline_dialog.py # Descarga del mapa offline con progreso
└── tests/                      # pytest: espacial, KML, tiles, cola de sync, servidor
```

## 2. Arquitectura

```
┌──────────── Flet (Python) ─────────────┐        ┌──────── WebView ────────┐
│ app.py  FAB / formulario / diálogos    │        │ map.js (Leaflet)        │
│   │ GPS (geolocator) → MapState        │        │  tiles/{z}/{x}/{y} ─────┼──┐
│   │ Connectivity → SyncWorker.set_online│ HTTP  │  api/layers, api/alerts │  │
│   ▼                                    │◄──────►│  api/state (polling 1 s)│  │
│ AppContext ── LocalMapServer (127.0.0.1/<token>/) ◄── api/events (long-press)│
│   ├── SpatialIndex (Shapely | puro)    │        └─────────────────────────┘  │
│   ├── Database (SQLite: alerts + sync_queue)                                 │
│   ├── TileProvider: MBTiles → red (y la guarda) → overzoom ◄─────────────────┘
│   └── SyncWorker (hilo) ── Supabase REST (upsert idempotente + Storage)
└────────────────────────────────────────┘
```

Decisiones clave:

* **Servidor local en vez de `file://`**: Leaflet pide los tiles por URL y el servidor los entrega desde el MBTiles sin internet. JS y Python se comunican con `fetch()`, que funciona igual en Android, iOS, macOS y web. El servidor solo escucha en 127.0.0.1 y todas sus rutas llevan un token aleatorio por sesión.
* **Outbox**: la alerta y su entrada en `sync_queue` se escriben en la misma transacción SQLite. Si la app se cierra justo después de guardar, el worker igual la sube.
* **Idempotencia**: el ID de cada alerta es un UUID generado en el dispositivo y el servidor hace upsert con `on_conflict=id`. Por eso un reintento nunca duplica una alerta. La URL de la foto queda guardada antes del upsert, así que la foto no se vuelve a subir.
* **Overzoom**: si te acercas más allá del zoom cacheado, el servidor recorta y escala el tile padre (Pillow). El mapa nunca queda en blanco dentro del área descargada.
* **Shapely es opcional**: no está en las dependencias móviles para no depender de binarios nativos en Android/iOS. El motor en Python puro da el mismo resultado; hay un test que compara ambos en una grilla de 729 puntos.

## 3. Puesta en marcha (paso a paso)

### 3.1 Entorno de desarrollo
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest                      # 24 tests
```

### 3.2 Ejecutar en el escritorio (modo web, con el mapa embebido)
```bash
cd src
PUNTORIESGO_FAKE_GPS="-34.2005,-70.7745" PUNTORIESGO_BACKEND=none flet run --web main.py
```
* `PUNTORIESGO_FAKE_GPS` simula el GPS, porque en escritorio no hay.
* En modo web el iframe del mapa captura los clics de los controles que tiene encima (es una limitación de Flutter web). Por eso, solo en ese modo, el botón "Registrar Alerta" va en una barra inferior y el mapa se oculta mientras hay un diálogo abierto. En Android/iOS el botón flota sobre el mapa.
* `flet run` en escritorio Linux/Windows (sin `--web`) no tiene WebView y muestra un botón para abrir el mapa en el navegador.

### 3.3 Probar en el teléfono
```bash
flet run --android src/main.py     # o --ios; requiere la app "Flet" del store
```

### 3.4 Compilar
```bash
flet build apk      # Android
flet build ipa      # iOS (en macOS con Xcode)
```
`pyproject.toml` ya declara los permisos (`location`, `camera`, `photo_library`). También declara `usesCleartextTraffic=true`, que hace falta para que el WebView de Android cargue `http://127.0.0.1`.

### 3.5 Configurar el backend (Supabase)
1. Crea la tabla `alerts` con el SQL que está en el docstring de `src/puntoriesgo/data/sync.py`. Crea también el bucket de Storage `alert-photos`.
2. Configura la app con variables de entorno o con `config.json` en el directorio de datos de la app:
   ```json
   { "supabase_url": "https://xxxx.supabase.co", "supabase_key": "<anon key>" }
   ```
   Con RLS activado, define políticas `insert`/`update` para el rol que use la app.
3. Para usar **Firebase o una API propia**, implementa `SyncBackend` (`is_reachable`, `upload_photo`, `upsert_alert`) y devuélvelo en `build_backend()`.

### 3.6 Cargar tu propio predio
* Desde la app: menú ⋮ → **Importar sectores (GeoJSON/KML)…**. El archivo se guarda y se vuelve a cargar al iniciar.
* O reemplaza `src/assets/data/sectores_riego.geojson`.
* Los polígonos se toman como **sectores** y los puntos como **equipos de riego**. Si una propiedad `layer`/`capa`/`tipo` o el nombre de la carpeta KML dice "sector" o "equipo/válvula/bomba", se respeta. El nombre se toma de `nombre`/`name`, y el id de `id`/`codigo`.

### 3.7 Mapa offline
* **En la app**: menú ⋮ → **Mapa offline…** → elige los zooms → *Calcular* → *Descargar*. Solo descarga los tiles que tocan cada sector más un margen (250 m por defecto), no todo el rectángulo del predio. Se puede cancelar y retomar después, y se detiene sola si se pierde la señal.
* **Desde un PC** (recomendado para áreas grandes):
  ```bash
  python scripts/precache_tiles.py --layer fundo.kml --min-zoom 12 --max-zoom 19 --out esri_world_imagery.mbtiles
  ```
  Copia el archivo a `<datos de la app>/tiles/esri_world_imagery.mbtiles`.
* Además, todo tile que se ve con conexión queda guardado en la caché.

> ⚠️ **Licencias**: la descarga masiva y el uso offline de ESRI World Imagery o Mapbox están sujetos a sus términos. Revisa que tu cuenta o plan lo permita. Para usar Mapbox: `PUNTORIESGO_TILE_SOURCE_ID=mapbox_satellite` y `PUNTORIESGO_MAPBOX_TOKEN=pk...`.

## 4. Flujo de usuario

1. **Pantalla principal**: mapa satelital a pantalla completa con los sectores (amarillo), los equipos de riego (azul) y tu posición GPS con su círculo de precisión. ◎ sigue tu posición y ⤢ muestra el predio completo.
2. **"+ Registrar Alerta"** usa la posición GPS (pide una nueva si la última tiene más de 30 s). **Mantener presionado el mapa** marca un riesgo visto a distancia.
3. **Formulario**: el sector y el equipo de riego más cercano (y también el del propio sector, si es otro) se llenan solos. Tú eliges tipo de problema, severidad, observaciones y foto. La foto se comprime en el teléfono.
4. **Visualización**: el color del pin indica la severidad (verde, amarillo, naranjo, rojo; las críticas pulsan) y el glifo indica el tipo. Un punto blanco marca las alertas aún no sincronizadas. La leyenda permite filtrar por severidad.

## 5. Configuración (`AppConfig`)

Todas las opciones se pueden fijar con `PUNTORIESGO_<NOMBRE>` o en `config.json`: `tile_source_id`, `mapbox_token`, `offline_min_zoom`, `offline_max_zoom`, `offline_buffer_m`, `offline_max_tiles`, `sector_tolerance_m` (si el GPS cae fuera de un sector, asigna el más cercano dentro de esa distancia), `backend`, `supabase_*`, `sync_interval_s`, `photo_max_side_px`, `photo_jpeg_quality`, `fake_gps`, `data_dir`.

## 6. Próximos pasos sugeridos
* Captura directa con cámara (`flet-camera`). Hoy se usa el selector del sistema, que permite tomar la foto o elegirla de la galería.
* Zonas de riesgo como polígonos, dibujadas en el mapa con Leaflet.draw.
* Bajar al teléfono las alertas de otros usuarios (sync bidireccional) y avisos push para las críticas.

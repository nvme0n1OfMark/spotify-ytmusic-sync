# Spotify ➜ YouTube Music (sync)

Script en Python que copia tus **Me gusta** y tus **playlists** de Spotify a YouTube Music dejándolas «igualitas»:

- 🎵 **Mismas canciones** — busca cada canción en YouTube Music por título, artista y duración.
- 🖼 **Misma portada** — descarga la portada de Spotify y la sube como imagen de la playlist en YouTube Music (incluida la de «Liked Songs»).
- 📝 **Mismo nombre y descripción**.
- ↷ **Mismo orden** — reordena en YouTube Music cuando hace falta (si añades una canción en medio de la lista en Spotify, la siguiente sync la coloca en su sitio).
- 🗑 **Sincroniza los borrados** — elimina de YouTube Music lo que ya no está en Spotify (desactivable con `--no-prune`, con salvaguarda contra borrados masivos).
- ♻️ **Incremental** — ejecuciones posteriores solo procesan lo nuevo (caché en `state.json`), así que puedes programarlo cada día.

Usa la API oficial de Spotify y [`ytmusicapi`](https://github.com/sigma67/ytmusicapi) (API interna, no oficial, de YouTube Music).

---

## 1) Requisitos previos

- Python 3.9 o superior (probado con 3.14).
- Cuenta de Spotify (las credenciales de desarrollo son gratuitas).
- Cuenta de YouTube Music.
- Para las **portadas**: el canal debe estar **verificado con teléfono** → https://www.youtube.com/verify. Después de verificar, renueva la sesión con `.venv/bin/python sync.py setup --refresh`. Si no está verificado, todo lo demás funciona igual; solo se omiten las portadas.

## 2) Instalación

```bash
cd ~/spotify-ytmusic-sync
python3 -m venv .venv              # ya está creado; solo si lo rehaces
.venv/bin/pip install -r requirements.txt
```

Pruebas offline (no usan red ni credenciales):

```bash
.venv/bin/python -m unittest discover -s tests -v
```

## 3) Credenciales de Spotify

1. Entra en https://developer.spotify.com/dashboard → **Create app** (nombre y descripción libres).
2. En la app, añade como *Redirect URI* exacto: `http://127.0.0.1:8888/callback`
3. Crea tu `config.env` y rellena las claves:

```bash
cp config.env.example config.env
nano config.env
```

```ini
SPOTIFY_CLIENT_ID=tu_client_id
SPOTIFY_CLIENT_SECRET=tu_client_secret
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8888/callback
```

> ⚠️ `config.env` contiene tu Client Secret. Ya está incluido en `.gitignore`: **no lo subas a GitHub**. Tampoco subas `state.json`, `.spotify_token`, `ytmusic_headers.json` ni `covers/`.

## 4) Conexión con YouTube Music

```bash
.venv/bin/python sync.py setup
```

- Spotify: la primera vez abre el navegador para autorizar.
- YouTube Music: te pedirá pegar unas cabeceras de tu navegador:
  1. Abre https://music.youtube.com con tu cuenta y recarga la página.
  2. `F12` → pestaña **Red / Network** → filtra por `browse` → haz clic en cualquier canción para que aparezca una petición `POST /youtubei/v1/browse`.
  3. Clic derecho sobre esa petición → **Copiar** → **Copiar encabezados de solicitud**.
  4. Pégalos en la terminal y pulsa **Ctrl-D** (no Enter) para terminar.

> ⚠️ Esas cabeceras caducan cada cierto tiempo (cerrar sesiones, cambiar contraseña, limpiar cookies…). Si `sync` avisa de que la sesión no es válida, vuelve a ejecutar `setup`. Si la sesión funciona pero quieres renovarla (por ejemplo, después de verificar el canal para las portadas), usa `.venv/bin/python sync.py setup --refresh`.

## 5) Uso

```bash
# 1º: ver qué haría, sin tocar nada
.venv/bin/python sync.py sync --dry-run

# Sincronizar de verdad
.venv/bin/python sync.py sync

# Solo las playlists cuyo nombre contenga «Rock»
.venv/bin/python sync.py sync --only "Rock"

# Resumen del estado guardado
.venv/bin/python sync.py status
```

La **primera** ejecución es la lenta (busca canción a canción). Las siguientes son rápidas: solo procesan lo nuevo. Si tienes una biblioteca enorme, puedes hacerlo por tandas con `--limit 500` (cada ejecución busca 500 canciones nuevas y las siguientes continúan donde quedó).

### Opciones de `sync`

| Opción | Qué hace |
|---|---|
| `--dry-run` | No cambia nada; solo informa |
| `--liked-mode like\|playlist\|both\|skip` | «Me gusta»: `like` (dar Me gusta en la lista oficial de YT; por defecto), `playlist` (crear playlist idéntica), `both` o `skip` |
| `--liked-playlist-name NOMBRE` | Nombre de la playlist de «Me gusta» (por defecto `Liked Songs (Spotify)`) |
| `--only TEXTO` / `--exclude TEXTO` | Filtra playlists por nombre (repetible) |
| `--skip-playlists` / `--skip-followed` | Solo «Me gusta» / ignorar playlists de otros |
| `--no-prune` | No borra en YT lo que ya no está en Spotify |
| `--force-prune` | Permite borrados de más de la mitad de la playlist (por defecto se bloquean) |
| `--no-covers` / `--no-order` | Desactiva portadas / orden idéntico |
| `--update-metadata` | También actualiza nombre/descripción/portada de playlists existentes que se reutilicen |
| `--privacy PRIVATE\|UNLISTED\|PUBLIC` | Privacidad de playlists nuevas (por defecto `PRIVATE`) |
| `--fresh` | 🔴 Borra TODAS tus playlists de YouTube Music (excepto «Me gusta» y listas del sistema) y las clona de nuevo desde cero. Antes comprueba que las portadas funcionan; si no, aborta sin borrar nada |
| `--require-covers` | Comprueba que YouTube permite poner portadas antes de sincronizar; si no, aborta sin borrar nada |
| `--no-cover-check` | No compruebes las portadas antes de `--fresh` (si fallan, las playlists se quedan sin portada) |
| `--limit N` | Máx. búsquedas nuevas por ejecución (0 = sin límite) |
| `--threshold 0.65` | Umbral de coincidencia (más alto = más estricto) |
| `--no-fallback-best` | Si no encuentra la canción, no pone la más parecida (por defecto **sí** la pone) |
| `--fallback-min 0.30` | Puntuación mínima para aceptar «la más parecida» |
| `--retry-unmatched` | Vuelve a buscar las sin coincidencia y las aproximadas |
| `--workers N` / `--delay S` | Búsquedas en paralelo / segundos entre peticiones |

> ⚠️ `--fresh` borra **todas** tus playlists propias de YouTube Music (no solo las que se llamen igual), conserva «Me gusta» y las listas del sistema, y luego vuelve a clonarlo todo desde Spotify. Si lo combinas con `--only`, `--exclude` o `--skip-playlists`, por seguridad solo borra las que coincidan por nombre y avisa con un warning.

## 6) ¿Cómo empareja las canciones?

Busca «título + artista» en YouTube Music y puntúa cada candidato con:

- **título** (55 %), **artista** (30 %) y **duración** (15 %),
- veto si el artista no coincide (evita covers de otros artistas),
- penalización a versiones añadidas («instrumental», «karaoke», «remix», «live»…): si no hay original, la versión se usa pero se marca como aproximada,
- pequeña preferencia por pistas «ATV» (audio oficial) frente a vídeos,
- entiende cualquier alfabeto (cirílico, japonés…) y translitera («Кино» ↔ «Kino»).

Se acepta el mejor candidato si supera `--threshold` (0.65 por defecto).

Si **ningún** candidato supera el umbral, el script pone **la más parecida** (si su puntuación llega a `--fallback-min`, 0.30 por defecto). Así no se queda ninguna canción fuera por poco; las aproximadas se marcan y se listan al final en `~/.config/spotify-ytmusic-sync/approximate.txt` para que las revises. Con `--no-fallback-best` se desactiva, y las que queden fuera se listan en `~/.config/spotify-ytmusic-sync/unmatched.txt`.

Al terminar la ejecución verás un **resumen con las canciones no encontradas y las aproximadas** (con su mejor candidato) y la ruta de los archivos con la lista completa.

- Si ves **falsos negativos** (no encontró canciones fáciles): `--threshold 0.55 --retry-unmatched`.
- Si ves **falsos positivos** (puso una versión equivocada): `--threshold 0.75`.
- Si no quieres aproximadas: `--no-fallback-best`.

## 7) Detalles y limitaciones

- La API de YouTube Music es **no oficial**: el script va con ritmo suave (0.35 s entre peticiones, 2 búsquedas en paralelo) para no llamar la atención del servidor.
- **Portadas**: se suben a `music.youtube.com` con la sesión del navegador. Requieren canal verificado por teléfono y archivos jpg/png (se usa la mejor resolución que da Spotify, normalmente 300–640 px).
- «Liked Music» de YouTube **no admite portada personalizada**; para tener la portada y el orden exactos de tus «Me gusta», el modo `playlist` crea una lista idéntica (con la portada oficial de Liked Songs).
- El **orden** se corrige con movimientos (`ACTION_MOVE_VIDEO_BEFORE`); si algo falla, se reintenta una vez y, si no, se avisa por consola.
- El **borrado** (`--prune`, activo por defecto) solo elimina canciones que ya no existen en la playlist de Spotify; si afectaría a más de la mitad de la playlist, se omite por seguridad (`--force-prune` lo permite).
- Si cambias en Spotify el nombre, la descripción, la portada o el orden, la siguiente `sync` lo refleja.
- **Canciones duplicadas**: si una playlist de Spotify tiene la misma canción repetida, también se copia repetida en YouTube Music. El total final incluye esas copias; la barra de progreso muestra el total real (únicas + duplicadas).
- **Barra de progreso**: en una terminal verás barras con porcentaje, velocidad y tiempo restante (búsquedas, añadir/quitar/reordenar canciones y marcar «Me gusta»). Si rediriges la salida (p. ej. cron), imprime una línea cada 25 elementos.
- Estado y caché: `~/.config/spotify-ytmusic-sync/` (`state.json`, `unmatched.txt`, `approximate.txt`, `covers/`, sesiones de Spotify y YouTube). Borra `state.json` solo si quieres empezar de cero.

## 8) Automatizarlo (opcional)

Cron diario a las 4:00:

```cron
0 4 * * * cd ~/spotify-ytmusic-sync && .venv/bin/python sync.py sync >> ~/.config/spotify-ytmusic-sync/cron.log 2>&1
```

## 9) Problemas frecuentes

| Síntoma | Solución |
|---|---|
| «La sesión de YouTube Music no es válida o ha caducado» | `.venv/bin/python sync.py setup` (vuelve a pegar cabeceras) |
| `redirect_uri_mismatch` de Spotify | El *Redirect URI* de tu app debe ser exactamente `http://127.0.0.1:8888/callback` |
| Error «puerto 8888 en uso» | Cierra lo que use ese puerto y repite `setup` |
| Las playlists salen sin portada | Verifica el canal en https://www.youtube.com/verify, ejecuta `.venv/bin/python sync.py setup --refresh` para renovar la sesión y reintenta con `.venv/bin/python sync.py sync --require-covers` (o quita `--no-covers` si la desactivaste) |
| «YouTube Music no respondió JSON (posible límite temporal)» | YouTube devolvió HTML por exceso de peticiones; el script espera y reintenta solo. Si se repite, espera unos minutos y vuelve a ejecutar: no se pierde nada |
| Muchas canciones «sin coincidencia» | `--retry-unmatched` y prueba `--threshold 0.55`; con la opción por defecto (más parecida) casi nunca debería quedar ninguna |
| Una aproximada no te convence | Búscala en `approximate.txt`, corrígela a mano en YT y ejecuta con `--no-fallback-best` (o sube `--fallback-min`) |
| Una canción quedó mal emparejada | Bórrala de la playlist en YT y añádela a mano, o sube `--threshold` |

> Alternativa: existe un paquete de la comunidad (`pip install spotify_to_ytmusic`) que hace una migración parecida, pero sin portadas, descripción ni orden idénticos.

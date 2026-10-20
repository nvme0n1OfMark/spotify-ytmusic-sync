#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync.py — Sincroniza Spotify ➜ YouTube Music.

Copia tus «Me gusta» y tus playlists de Spotify a YouTube Music dejándolas
igualitas: mismo nombre, misma descripción, misma portada, mismas canciones
y mismo orden (y opcionalmente elimina en YouTube lo que ya no está en Spotify).

Comandos:
    setup   Configura tus credenciales de Spotify y de YouTube Music.
    sync    Sincroniza (usa --dry-run para ver qué haría sin tocar nada).
    status  Muestra un resumen del estado guardado.

Primeros pasos (ver README.md):
    .venv/bin/python sync.py setup
    .venv/bin/python sync.py sync --dry-run
    .venv/bin/python sync.py sync
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import re
import sys
import threading
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

import requests
import spotipy
from spotipy.exceptions import SpotifyException
from spotipy.oauth2 import SpotifyOAuth
from ytmusicapi import YTMusic
from ytmusicapi.auth.types import AuthType
from ytmusicapi.models.content.enums import LikeStatus

APP_NAME = "spotify-ytmusic-sync"
SPOTIFY_SCOPES = "user-library-read playlist-read-private playlist-read-collaborative"

# Portada oficial de «Liked Songs» de Spotify (para clonarla en YT Music).
LIKED_COVER_URLS = [
    "https://misc.scdn.co/liked-songs/liked-songs-1080.png",
    "https://misc.scdn.co/liked-songs/liked-songs-640.png",
]

YT_PLAYLIST_NAME_MAX = 150
YT_DESC_MAX = 1500
ADD_CHUNK = 50
MOVE_CHUNK = 25
REMOVE_CHUNK = 50
MAX_IMAGE_BYTES = 10 * 1024 * 1024

log = logging.getLogger("sync")


def _fmt_time(seconds: float) -> str:
    seconds = int(max(0, seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"
    if seconds >= 60:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds}s"


_ACTIVE_BAR = None


class ProgressBar:
    """Barra de progreso para terminal; se desactiva sola si la salida no es una terminal."""

    def __init__(self, total: int, prefix: str = "", unit: str = "", width: int = 26, stream=None):
        global _ACTIVE_BAR
        self.total = max(1, int(total))
        self.prefix = prefix
        self.unit = unit
        self.width = width
        self.stream = stream or sys.stderr
        self.enabled = hasattr(self.stream, "isatty") and self.stream.isatty()
        self.n = 0
        self.start = time.monotonic()
        self._suffix = ""
        self._last_len = 0
        self._last_draw = 0.0
        self._lock = threading.RLock()
        if self.enabled:
            _ACTIVE_BAR = self

    def update(self, n: int = 1, suffix: str = "") -> None:
        with self._lock:
            self.n += n
            self._draw_locked(suffix=suffix)

    def close(self, suffix: str = "") -> None:
        global _ACTIVE_BAR
        with self._lock:
            if self.enabled:
                self._draw_locked(suffix=suffix, force=True)
                self.stream.write("\n")
                self.stream.flush()
                self._last_len = 0
            if _ACTIVE_BAR is self:
                _ACTIVE_BAR = None

    def paused(self):
        return _BarPause(self) if self.enabled else _BarPause(None)

    def _clear_locked(self) -> None:
        if self._last_len:
            self.stream.write("\r" + " " * self._last_len + "\r")
            self.stream.flush()
            self._last_len = 0

    def _draw_locked(self, suffix: str = "", force: bool = False) -> None:
        if not self.enabled:
            return
        self._suffix = suffix or self._suffix
        now = time.monotonic()
        if not force and now - self._last_draw < 0.1:
            return
        self._last_draw = now
        done = min(self.n, self.total)
        frac = done / self.total
        filled = int(self.width * frac)
        bar = "█" * filled + "░" * (self.width - filled)
        elapsed = now - self.start
        rate = done / elapsed if elapsed > 0 else 0.0
        eta = (self.total - done) / rate if rate > 0 else 0.0
        line = f"{self.prefix} {bar} {done}/{self.total} {frac * 100:3.0f}% "
        line += f"{rate:4.1f} {self.unit}/s" if self.unit else f"{rate:4.1f}/s"
        if done < self.total:
            line += f" ETA {_fmt_time(eta)}"
        if self._suffix:
            line += f"  {self._suffix}"
        pad = " " * max(0, self._last_len - len(line))
        self.stream.write("\r" + line + pad)
        self.stream.flush()
        self._last_len = len(line)


class _BarPause:
    """Apaga la barra mientras se escribe un log y la vuelve a dibujar después."""

    def __init__(self, bar):
        self.bar = bar

    def __enter__(self):
        if self.bar is not None:
            self.bar._lock.acquire()
            self.bar._clear_locked()
        return self

    def __exit__(self, *exc):
        if self.bar is not None:
            try:
                self.bar._draw_locked(force=True)
            finally:
                self.bar._lock.release()
        return False


class _BarAwareHandler(logging.StreamHandler):
    """Handler de logs que aparta la barra de progreso antes de escribir."""

    def emit(self, record):
        bar = _ACTIVE_BAR
        if bar is not None and bar.enabled:
            with bar.paused():
                super().emit(record)
        else:
            super().emit(record)


class SyncError(Exception):
    """Error de negocio / configuración (normalmente no reintentable)."""


def setup_logging(verbose: bool) -> None:
    handler = _BarAwareHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("sync")
    logger.handlers[:] = [handler]
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logging.getLogger("ytmusicapi").setLevel(logging.WARNING)


def app_dir() -> Path:
    p = os.environ.get("SPOTIFY_YTMUSIC_CONFIG_DIR") or str(Path.home() / ".config" / APP_NAME)
    return Path(p).expanduser()


def chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i : i + n]


_FATAL_MARKERS = ("bad request", "duplicate", "invalid", "unauthenticated", "permission denied")
_FATAL_CODE_RE = re.compile(r"\b(?:400|401|403|404)\b")


def call_retries(desc: str, fn, *args, retries: int = 3, base: float = 1.2, **kwargs):
    """Ejecuta fn(*args) con reintentos y espera exponencial."""
    for attempt in range(max(retries + 1, 6)):
        try:
            return fn(*args, **kwargs)
        except (KeyboardInterrupt, SystemExit):
            raise
        except json.JSONDecodeError as exc:
            # ytmusicapi intenta parsear como JSON incluso cuando YouTube devuelve
            # una página HTML de «demasiadas peticiones». No es un fallo real:
            # esperamos más y reintentamos.
            if attempt >= 5:
                raise SyncError(
                    "YouTube Music devolvió HTML en vez de JSON (posible límite temporal). "
                    "Espera unos minutos y vuelve a ejecutar; no se perdió nada."
                ) from exc
            wait = 5.0 * (2 ** attempt) + random.random() * 0.5
            log.warning(
                "   ⏳ YouTube Music no respondió JSON (posible límite temporal); "
                "espero %.0f s y reintento (%d/5)…",
                wait,
                attempt + 1,
            )
            time.sleep(wait)
        except Exception as exc:  # noqa: BLE001
            low = str(exc).lower()
            fatal = any(m in low for m in _FATAL_MARKERS) or bool(_FATAL_CODE_RE.search(low))
            if attempt >= retries or fatal:
                raise
            wait = base * (2 ** attempt) + random.random() * 0.5
            if isinstance(exc, SpotifyException) and getattr(exc, "http_status", None) == 429:
                try:
                    wait = float(exc.headers.get("Retry-After", wait))
                except Exception:  # noqa: BLE001
                    pass
            log.debug("      ↻ %s: reintento %d/%d en %.1fs (%s)", desc, attempt + 1, retries, wait, exc)
            time.sleep(wait)


class Pacer:
    """Limita la frecuencia de llamadas a la API."""

    def __init__(self, delay: float):
        self.delay = max(0.0, delay)
        self._next = 0.0

    def wait(self) -> None:
        if self.delay <= 0:
            return
        now = time.monotonic()
        if now < self._next:
            time.sleep(self._next - now)
        self._next = time.monotonic() + self.delay


# ------------------------------- Configuración -------------------------------


def parse_env_file(path: Path) -> dict:
    values: dict = {}
    if not path or not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def build_config(args) -> dict:
    adir = app_dir()
    values: dict = {}
    candidates = []
    if getattr(args, "config", None):
        candidates.append(Path(args.config).expanduser())
    else:
        candidates.append(Path.cwd() / "config.env")
        candidates.append(adir / "config.env")
    for cand in candidates:
        values.update(parse_env_file(cand))
    for key in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "SPOTIFY_REDIRECT_URI"):
        if os.environ.get(key):
            values[key] = os.environ[key]
    values.setdefault("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
    values["app_dir"] = adir
    return values


# ---------------------------------- Estado -----------------------------------


def load_state(path: Path) -> dict:
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("version", 1)
                data.setdefault("tracks", {})
                data.setdefault("liked", {})
                data.setdefault("playlists", {})
                return data
        except Exception as exc:  # noqa: BLE001
            log.warning("⚠ El estado guardado estaba corrupto (%s); empiezo de cero.", exc)
            try:
                path.replace(path.with_suffix(".json.corrupt"))
            except OSError:
                pass
    return {"version": 1, "tracks": {}, "liked": {}, "playlists": {}}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    state["last_run"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# ------------------------------ Canciones y matching -------------------------


@dataclass
class Track:
    sid: str
    name: str
    artists: list
    album: str
    duration_s: int

    @property
    def label(self) -> str:
        return f"{', '.join(self.artists)} — {self.name}"


_QUALIFIER_RE = re.compile(
    r"\s*[-–]\s*(?:(?:\d{4}\s+)?(?:digital\s+)?remaster(?:ed)?(?:\s+\d{4})?|live|radio edit|"
    r"single version|album version|deluxe(?: edition)?|bonus track|mono|stereo|demo|instrumental|"
    r"acoustic|remix|edit|explicit)\b.*$",
    re.IGNORECASE,
)
_FEAT_RE = re.compile(r"\b(?:feat|ft|featuring)\.?\s+.*$", re.IGNORECASE)


# Transliteración cirílica → latina: así «Кино» casa con «Kino» y «Планка» consigo misma.
_TRANSLIT = str.maketrans(
    {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
        "з": "z", "и": "i", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
        "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h",
        "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y",
        "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
)


def norm(text: str) -> str:
    """Normaliza conservando letras de cualquier alfabeto (cirílico, japonés, etc.)."""
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.casefold().translate(_TRANSLIT)
    text = re.sub(r"[\W_]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def norm_strict(text: str) -> str:
    """Como norm() pero quitando además paréntesis/corchetes y sufijos tipo «(remaster)», «feat…»."""
    if not text:
        return ""
    text = re.sub(r"\([^()]*\)|\[[^\[\]]*\]", " ", text)
    text = _QUALIFIER_RE.sub("", text)
    text = _FEAT_RE.sub("", text)
    return norm(text)


def _sim_text(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


# «No latino» = fuera del ASCII y de las extensiones latinas (así «œ», «ø» cuentan como latinas).
_NONLATIN_RE = re.compile(r"[^\x00-\x7f\u00c0-\u024f\u1e00-\u1eff]")


def similarity(a: str, b: str) -> float:
    """Similitud 0-1 entre dos textos; usa la mejor variante (normal o estricta)."""
    if not a or not b:
        return 0.0
    na, nb = norm(a), norm(b)
    best = max(_sim_text(na, nb), _sim_text(norm_strict(a), norm_strict(b)))
    # Escrituras no latinas (kanji, cirílico…): si el texto corto está contenido
    # entero en el largo, lo damos por casi equivalente («青春狂騒曲» ⊂ «青春狂騒曲 - Seishun…»).
    shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
    if len(shorter) >= 4 and _NONLATIN_RE.search(shorter):
        matched = sum(b.size for b in SequenceMatcher(None, shorter, longer).get_matching_blocks())
        best = max(best, 0.9 * matched / len(shorter))
    return best


_STRONG_VARIANTS = frozenset({"karaoke", "tribute", "cover", "カラオケ"})
_SOFT_VARIANTS = frozenset({
    "instrumental", "remix", "live", "acoustic", "slowed", "sped", "speed", "nightcore",
    "mashup", "extended", "demo", "reverb", "edit", "インスト", "弾き語り",
})
_VERSION_WORDS = frozenset({"part", "pt", "chapter", "vol", "volume", "ver", "version"})
_WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)
_VERSION_NUM_RE = re.compile(r"\b(?:part|pt|chapter|vol|volume|ver|version)\.?\s*(\d+)")


def _plural_variants(words: set) -> set:
    out = set(words)
    for w in words:
        if w.endswith("es"):
            out.add(w[:-2])
        if w.endswith("s"):
            out.add(w[:-1])
    return out


def _variant_penalty(track: Track, result: dict) -> float:
    """Penaliza candidatos que añaden variantes («instrumental», «karaoke», «Part 3»…) respecto al original."""
    want_text = (track.name + " " + " ".join(track.artists)).casefold()
    cand_text = ((result.get("title") or "") + " " + " ".join(
        a.get("name") or "" for a in result.get("artists") or []
    )).casefold()
    want = set(_WORD_RE.findall(want_text))
    got = set(_WORD_RE.findall(cand_text))
    extra = _plural_variants(got - want)
    if extra & _STRONG_VARIANTS:
        return 0.55
    # «Part 3» vs «Part 2» (o una parte que solo tiene un lado) = otra grabación/canción.
    if (want ^ got) & _VERSION_WORDS or set(_VERSION_NUM_RE.findall(want_text)) != set(_VERSION_NUM_RE.findall(cand_text)):
        return 0.6
    if extra & _SOFT_VARIANTS:
        return 0.6  # por debajo del umbral: si no hay original, se usa pero queda «aproximada»
    return 1.0


def score_result(track: Track, result: dict, with_penalty: bool = False):
    """Puntúa (0-1) qué tan probable es que `result` de YT Music sea `track`."""
    title = similarity(track.name, result.get("title") or "")
    yt_artists = [a.get("name") or "" for a in (result.get("artists") or []) if a.get("name")]
    artist = 0.0
    for name in track.artists:
        for cand in yt_artists:
            artist = max(artist, similarity(name, cand))
    dur = result.get("duration_seconds")
    if dur is None or track.duration_s <= 0:
        dur_factor = 0.75
    else:
        delta = abs(int(dur) - track.duration_s)
        dur_factor = 1.0 if delta <= 3 else 0.9 if delta <= 8 else 0.6 if delta <= 20 else 0.25 if delta <= 45 else 0.05
    score = 0.55 * title + 0.30 * artist + 0.15 * dur_factor
    if artist < 0.55:  # casi seguro es una versión/cover de otro artista
        score *= 0.55
    pen = _variant_penalty(track, result)
    score *= pen
    if result.get("videoType") == "MUSIC_VIDEO_TYPE_ATV":
        score += 0.02
    return (score, pen) if with_penalty else score


def best_match(yt, track: Track, threshold: float):
    """Busca en YT Music y devuelve (videoId|None, score, descripción, id del mejor candidato).

    `videoId` es None si el mejor candidato no llega al umbral; aun así se devuelve
    el cuarto valor con el mejor candidato encontrado (para usar «la más parecida»).
    """
    query = f"{track.name} {track.artists[0]}".strip()
    best_score, best_res, best_pen = 0.0, None, 1.0
    for flt in ("songs", "videos"):
        results = yt.search(query, flt)
        for res in results or []:
            if not res.get("videoId"):
                continue
            s, pen = score_result(track, res, with_penalty=True)
            if s > best_score:
                best_score, best_res, best_pen = s, res, pen
        # No cortamos si el mejor candidato llevaba penalización de variante
        # («instrumental», «live»…): mejor mirar también los videos por si está el original.
        if best_score >= threshold and best_pen == 1.0:
            break
    best_id = best_res.get("videoId") if best_res else None
    video_id = best_id if (best_res and best_score >= threshold) else None
    desc = ""
    if best_res:
        names = ", ".join(a.get("name") or "" for a in best_res.get("artists") or [])
        desc = f"{best_res.get('title')} — {names} ({best_score:.2f})"
    return video_id, best_score, desc, best_id


# ---------------------------------- Spotify ----------------------------------


def parse_track(raw: Optional[dict]) -> Optional[Track]:
    if not raw or raw.get("type") != "track" or raw.get("is_local"):
        return None
    artists = [a.get("name") for a in raw.get("artists") or [] if a.get("name")]
    if not artists or not raw.get("id"):
        return None
    return Track(
        sid=raw["id"],
        name=raw.get("name") or "",
        artists=artists,
        album=(raw.get("album") or {}).get("name") or "",
        duration_s=int((raw.get("duration_ms") or 0) / 1000),
    )


class SpotifySource:
    def __init__(self, cfg: dict, pace: Pacer, token_path: Path):
        client_id = cfg.get("SPOTIFY_CLIENT_ID")
        client_secret = cfg.get("SPOTIFY_CLIENT_SECRET")
        if not client_id or not client_secret:
            raise SyncError(
                "Faltan SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET. Crea una app en "
                "https://developer.spotify.com/dashboard y pon las claves en config.env "
                "(o ejecuta: python sync.py setup)."
            )
        self.pace = pace
        self.auth = SpotifyOAuth(
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=cfg.get("SPOTIFY_REDIRECT_URI") or "http://127.0.0.1:8888/callback",
            scope=SPOTIFY_SCOPES,
            cache_path=str(token_path),
            open_browser=True,
        )
        self.sp = spotipy.Spotify(auth_manager=self.auth)

    def call(self, desc: str, fn, *args, **kwargs):
        self.pace.wait()
        return call_retries(desc, fn, *args, **kwargs)

    def me(self) -> dict:
        return self.call("perfil de Spotify", self.sp.current_user)

    def _paged(self, desc: str, fn, **kwargs):
        offset = 0
        while True:
            page = self.call(desc, fn, offset=offset, **kwargs)
            items = page.get("items") or []
            yield from items
            if not page.get("next") or not items:
                break
            offset += len(items)

    def liked_tracks(self) -> list:
        out = []
        for item in self._paged("Me gusta", self.sp.current_user_saved_tracks, limit=50):
            track = parse_track((item or {}).get("track"))
            if track:
                out.append(track)
        return out

    def playlists(self) -> list:
        out = []
        for item in self._paged("playlists", self.sp.current_user_playlists, limit=50):
            if not item or not item.get("id"):
                continue
            images = sorted(item.get("images") or [], key=lambda im: im.get("width") or 0, reverse=True)
            out.append(
                {
                    "id": item["id"],
                    "name": item.get("name") or "Playlist",
                    "description": (item.get("description") or "").strip(),
                    "owner_id": ((item.get("owner") or {}).get("id")) or "",
                    "image_urls": [im.get("url") for im in images if im.get("url")],
                    "total": ((item.get("tracks") or {}).get("total")) or 0,
                }
            )
        return out

    def playlist_tracks(self, playlist_id: str) -> list:
        out = []
        for item in self._paged(
            "pistas de la playlist",
            self.sp.playlist_items,
            playlist_id=playlist_id,
            limit=100,
            additional_types="track",
        ):
            if not item:
                continue
            raw = item.get("item") if "item" in item else item.get("track")
            track = parse_track(raw)
            if track:
                out.append(track)
        return out


# ------------------------------- YouTube Music -------------------------------


def _status_ok(resp) -> bool:
    if not isinstance(resp, dict):
        return True
    status = resp.get("status")
    return not status or "SUCCEEDED" in str(status)


def _as_count(value) -> int:
    """Normaliza el conteo de la biblioteca: YT a veces lo da como «1,218»."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    digits = re.findall(r"\d+", str(value or ""))
    return int("".join(digits)) if digits else 0


def _walk_values(obj, key):
    """Recorre dicts/listas y devuelve los valores de una clave (sin exponer el resto)."""
    if isinstance(obj, dict):
        for k, value in obj.items():
            if k == key:
                yield value
            yield from _walk_values(value, key)
    elif isinstance(obj, (list, tuple)):
        for item in obj:
            yield from _walk_values(item, key)


def _deletion_confirmed(resp, playlist_id: str) -> bool:
    """True si la respuesta de YT contiene la orden de borrado de esa playlist."""
    if isinstance(resp, str):
        return not resp.strip() or "SUCCEEDED" in resp.upper()
    for command in _walk_values(resp, "handlePlaylistDeletionCommand"):
        if not isinstance(command, dict):
            continue
        deleted_id = command.get("playlistId")
        if not deleted_id or deleted_id == playlist_id:
            return True
    return False


def _short_resp(resp, limit: int = 300) -> str:
    """Resumen seguro de una respuesta de error: nunca vuelca tokens de sesión."""
    if isinstance(resp, dict):
        for key in ("status", "error", "message", "reason"):
            value = resp.get(key)
            if isinstance(value, dict):
                value = value.get("message") or value.get("status") or value.get("code")
            if value:
                text = str(value)
                return text[:limit] + ("…" if len(text) > limit else "")
        keys = ", ".join(str(k) for k in list(resp)[:6])
        return f"respuesta sin estado ({keys})"
    text = str(resp)
    return text[:limit] + ("…" if len(text) > limit else "")


def upload_playlist_image(yt: YTMusic, image_path: Path) -> str:
    """Sube una portada con el protocolo resumable-upload de Google y devuelve su blob id.

    Replica la función que ytmusicapi aún no incluye en su versión de PyPI
    (PRs sigma67/ytmusicapi#938 y #947).
    """
    fp = Path(image_path)
    if not fp.is_file():
        raise SyncError(f"No existe la imagen: {fp}")
    if fp.suffix.lower().lstrip(".") not in ("jpg", "jpeg", "png"):
        raise SyncError(f"Formato de imagen no soportado: {fp.suffix} (usa jpg o png)")
    size = fp.stat().st_size
    if size >= MAX_IMAGE_BYTES:
        raise SyncError(f"La imagen {fp.name} supera el límite de 10 MB")
    headers = dict(getattr(yt, "headers", None) or {})
    if not headers:
        raise SyncError("La sesión de YouTube Music no tiene cabeceras; vuelve a ejecutar `setup`.")
    headers.pop("content-encoding", None)
    headers["content-type"] = "application/x-www-form-urlencoded;charset=utf-8"
    headers["X-Goog-Upload-Command"] = "start"
    headers["X-Goog-Upload-Header-Content-Length"] = str(size)
    headers["X-Goog-Upload-Protocol"] = "resumable"
    endpoint = "https://music.youtube.com/playlist_image_upload/playlist_custom_thumbnail"
    proxies = getattr(yt, "proxies", None)
    resp = requests.post(endpoint, data="", headers=headers, proxies=proxies, timeout=60)
    upload_url = resp.headers.get("X-Goog-Upload-URL")
    if not upload_url:
        raise SyncError(f"Subida de portada: respuesta inesperada ({resp.status_code}) {resp.text[:200]}")
    headers["X-Goog-Upload-Command"] = "upload, finalize"
    headers["X-Goog-Upload-Offset"] = "0"
    with open(fp, "rb") as fh:
        resp = requests.post(upload_url, data=fh, headers=headers, proxies=proxies, timeout=300)
    if resp.status_code != 200:
        raise SyncError(f"Subida de portada falló ({resp.status_code}): {resp.text[:200]}")
    try:
        blob = resp.json().get("encryptedBlobId")
    except Exception:  # noqa: BLE001
        blob = None
    if not blob:
        raise SyncError("Subida de portada: la respuesta no trae encryptedBlobId")
    return blob


class YTMTarget:
    def __init__(self, auth_path: Path, pace: Pacer):
        self.auth_path = Path(auth_path)
        if not self.auth_path.is_file():
            raise SyncError(
                f"No existe el archivo de sesión de YouTube Music ({self.auth_path}). "
                "Ejecuta: python sync.py setup"
            )
        self.pace = pace
        self.yt = YTMusic(str(self.auth_path))
        if self.yt.auth_type == AuthType.UNAUTHORIZED:
            raise SyncError(
                "La sesión de YouTube Music no es válida o ha caducado. Vuelve a ejecutar: python sync.py setup"
            )
        self._library_cache = None

    def call(self, desc: str, fn, *args, **kwargs):
        self.pace.wait()
        return call_retries(desc, fn, *args, **kwargs)

    def account_label(self) -> str:
        info = self.call("cuenta de YouTube Music", self.yt.get_account_info)
        if isinstance(info, dict):
            for key in ("channelHandle", "accountName", "name", "channelId"):
                if info.get(key):
                    return str(info[key])
        return "conectado"

    def search(self, query: str, flt: str) -> list:
        return self.call("búsqueda", self.yt.search, query, filter=flt, limit=12)

    def library_playlists(self) -> list:
        if self._library_cache is None:
            raw = self.call("biblioteca de playlists", self.yt.get_library_playlists, limit=None)
            self._library_cache = [
                {
                    "id": p.get("playlistId") or "",
                    "title": p.get("title") or "",
                    "count": _as_count(p.get("count")),
                    # «owned» = tuya (editable/borrable); «Me gusta» y listas del sistema vienen en False.
                    "owned": bool(p.get("owned", True)),
                }
                for p in raw or []
                if p.get("playlistId")
            ]
        return self._library_cache

    def refresh_library(self) -> None:
        self._library_cache = None

    def find_library_playlist(self, title: str, exclude_ids: set):
        want = title.strip().casefold()
        for pl in self.library_playlists():
            if pl["id"] in exclude_ids or not pl["owned"]:
                continue
            if pl["title"].strip().casefold() == want:
                return pl
        return None

    def get_items(self, playlist_id: str) -> list:
        raw = self.call("leer playlist de YouTube", self.yt.get_playlist, playlist_id, limit=None)
        items = []
        for t in (raw or {}).get("tracks") or []:
            if t.get("videoId") and t.get("setVideoId"):
                items.append({"videoId": t["videoId"], "setVideoId": t["setVideoId"]})
        return items

    def create_playlist(self, title: str, description: str, privacy: str) -> str:
        res = self.call("crear playlist", self.yt.create_playlist, title, description, privacy_status=privacy)
        if isinstance(res, dict):
            raise SyncError(f"YouTube Music no creó la playlist: {_short_resp(res)}")
        return str(res)

    def delete_playlist(self, playlist_id: str) -> None:
        res = self.call("borrar playlist", self.yt.delete_playlist, playlist_id)
        # ytmusicapi devuelve un dict (sin «status») aunque el borrado SÍ se hizo.
        # Si la respuesta trae la orden de borrado, es éxito inmediato; si no,
        # confirmamos contra la biblioteca con reintentos (YT tarda en reflejarlo).
        if _deletion_confirmed(res, playlist_id):
            self._library_cache = None
            return
        for _ in range(6):
            self._library_cache = None
            if not any(pl["id"] == playlist_id for pl in self.library_playlists()):
                return
            time.sleep(0.8)
        raise SyncError("YouTube Music no borró la playlist (sigue en tu biblioteca)")

    def add_items(self, playlist_id: str, video_ids: list, duplicates: bool = False) -> None:
        video_ids = list(video_ids)
        bar = ProgressBar(len(video_ids), prefix="  ➕ Añadiendo") if len(video_ids) > ADD_CHUNK else None
        try:
            for chunk in chunks(video_ids, ADD_CHUNK):
                res = self.call(
                    "añadir canciones", self.yt.add_playlist_items, playlist_id, videoIds=chunk, duplicates=duplicates
                )
                if not _status_ok(res):
                    raise SyncError(f"YouTube Music rechazó añadir canciones: {_short_resp(res)}")
                if bar:
                    bar.update(len(chunk))
        finally:
            if bar:
                bar.close(suffix=f"{len(video_ids)} canciones")

    def remove_items(self, playlist_id: str, items: list) -> None:
        items = list(items)
        bar = ProgressBar(len(items), prefix="  🗑 Quitando") if len(items) > REMOVE_CHUNK else None
        try:
            for chunk in chunks(items, REMOVE_CHUNK):
                res = self.call("quitar canciones", self.yt.remove_playlist_items, playlist_id, chunk)
                if not _status_ok(res):
                    raise SyncError(f"YouTube Music rechazó quitar canciones: {_short_resp(res)}")
                if bar:
                    bar.update(len(chunk))
        finally:
            if bar:
                bar.close(suffix=f"{len(items)} canciones")

    def move_items(self, playlist_id: str, moves: list) -> None:
        """moves: [(setVideoId_a_mover, setVideoId_sucesor)] → mueve el primero justo antes del segundo."""
        moves = list(moves)
        bar = ProgressBar(len(moves), prefix="  ↷ Reordenando") if len(moves) > MOVE_CHUNK else None
        try:
            for chunk in chunks(moves, MOVE_CHUNK):
                actions = []
                for item, successor in chunk:
                    action = {"action": "ACTION_MOVE_VIDEO_BEFORE", "setVideoId": item}
                    if successor:
                        action["movedSetVideoIdSuccessor"] = successor
                    actions.append(action)
                body = {"playlistId": playlist_id, "actions": actions}
                res = self.call("reordenar", self.yt._send_request, "browse/edit_playlist", body)
                if not _status_ok(res):
                    raise SyncError(f"YouTube Music rechazó reordenar: {_short_resp(res)}")
                if bar:
                    bar.update(len(chunk))
        finally:
            if bar:
                bar.close(suffix=f"{len(moves)} movimientos")

    def rate(self, video_id: str, like: bool) -> None:
        res = self.call(
            "Me gusta en YouTube", self.yt.rate_song, video_id, LikeStatus.LIKE if like else LikeStatus.INDIFFERENT
        )
        if isinstance(res, dict) and "SUCCEEDED" not in str(res.get("status", "SUCCEEDED")):
            raise SyncError(f"No pude actualizar Me gusta: {_short_resp(res)}")

    def edit_metadata(self, playlist_id: str, title=None, description=None, image_path=None) -> None:
        actions = []
        if image_path is not None:
            blob = upload_playlist_image(self.yt, Path(image_path))
            actions.append(
                {
                    "action": "ACTION_SET_CUSTOM_THUMBNAIL",
                    "addedCustomThumbnail": {
                        "imageKey": {"type": "PLAYLIST_IMAGE_TYPE_CUSTOM_THUMBNAIL", "name": "studio_square_thumbnail"},
                        "playlistScottyEncryptedBlobId": blob,
                    },
                }
            )
        if title is not None:
            actions.append({"action": "ACTION_SET_PLAYLIST_NAME", "playlistName": title})
        if description is not None:
            actions.append({"action": "ACTION_SET_PLAYLIST_DESCRIPTION", "playlistDescription": description})
        if not actions:
            return
        body = {"playlistId": playlist_id, "actions": actions}

        def _send(body_):
            res = self.call("actualizar playlist", self.yt._send_request, "browse/edit_playlist", body_)
            if not _status_ok(res):
                raise SyncError(f"YouTube Music rechazó actualizar la playlist: {_short_resp(res)}")

        try:
            _send(body)
        except Exception:  # noqa: BLE001
            if description == "":  # algunos servidores rechazan descripciones vacías
                body = {
                    "playlistId": playlist_id,
                    "actions": [a for a in actions if a.get("action") != "ACTION_SET_PLAYLIST_DESCRIPTION"],
                }
                _send(body)
            else:
                raise


# ---------------------------------- Portadas ---------------------------------


def download_image(url: str, dest_dir: Path):
    resp = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0 (spotify-ytmusic-sync)"})
    resp.raise_for_status()
    data = resp.content
    if not data:
        raise SyncError("imagen vacía")
    sha = hashlib.sha1(data).hexdigest()
    ext = ".png" if "png" in (resp.headers.get("Content-Type") or "").lower() else ".jpg"
    path = dest_dir / f"{sha[:16]}{ext}"
    if not path.is_file():
        path.write_bytes(data)
    return path, sha


def make_liked_cover(dest_dir: Path):
    """Portada de reserva para «Liked Songs»: degradado morado + corazón."""
    try:
        from PIL import Image, ImageDraw
    except Exception:  # noqa: BLE001
        return None, None
    size = 640
    img = Image.new("RGB", (size, size))
    draw = ImageDraw.Draw(img)
    for y in range(size):
        t = y / (size - 1)
        draw.line(
            [(0, y), (size, y)],
            fill=(int(69 + (163 - 69) * t), int(10 + (120 - 10) * t), int(245 + (255 - 245) * t)),
        )
    cx, cy, r = size // 2, int(size * 0.40), 92
    draw.ellipse([cx - 2 * r + 20, cy - r, cx + 20, cy + r], fill="white")
    draw.ellipse([cx - 20, cy - r, cx + 2 * r - 20, cy + r], fill="white")
    draw.polygon(
        [(cx - 2 * r + 40, cy + int(r * 0.55)), (cx + 2 * r - 40, cy + int(r * 0.55)), (cx, int(size * 0.78))],
        fill="white",
    )
    path = dest_dir / "liked-songs.png"
    img.save(path, "PNG")
    return path, hashlib.sha1(path.read_bytes()).hexdigest()


def fetch_cover(urls: list, dest_dir: Path, liked_fallback: bool = False):
    """Devuelve (ruta_local, sha1, url_usada) o (None, None, None)."""
    for url in urls or []:
        try:
            path, sha = download_image(url, dest_dir)
            return path, sha, url
        except Exception as exc:  # noqa: BLE001
            log.debug("      ⚠ no pude descargar la portada %s (%s)", url, exc)
    if liked_fallback:
        path, sha = make_liked_cover(dest_dir)
        if path:
            return path, sha, None
    return None, None, None


# ------------------------------- Resolución ----------------------------------


class Resolver:
    """Busca cada canción de Spotify en YouTube Music, con caché persistente."""

    def __init__(
        self,
        make_yt,
        state: dict,
        threshold: float,
        retry_unmatched: bool,
        limit: int,
        workers: int,
        fallback_best: bool = True,
        fallback_min: float = 0.30,
    ):
        self._make_yt = make_yt
        self.state = state
        self.threshold = threshold
        self.retry_unmatched = retry_unmatched
        self.budget = limit or None
        self.workers = max(1, workers)
        self.fallback_best = fallback_best
        self.fallback_min = fallback_min
        self.lock = threading.Lock()
        self._tl = threading.local()
        self.searched = 0
        self.matched = 0
        self.unmatched = 0
        self.errors = 0
        self.approx = 0
        self.approx_info: dict = {}
        self.limit_skipped = 0
        self.reasons: dict = {}
        self._progress = 0
        self.bar = None
        self._bar_base = (0, 0, 0, 0)

    def _yt(self):
        yt = getattr(self._tl, "yt", None)
        if yt is None:
            yt = self._make_yt()
            self._tl.yt = yt
        return yt

    def resolve(self, tracks: list) -> dict:
        unique: dict = {}
        for track in tracks:
            unique.setdefault(track.sid, track)
        out: dict = {}
        todo = []
        cached = 0
        with self.lock:
            for sid, track in unique.items():
                entry = self.state["tracks"].get(sid)
                if (
                    entry
                    and entry.get("status") == "matched"
                    and entry.get("videoId")
                    and not (self.retry_unmatched and entry.get("fallback"))
                ):
                    out[sid] = entry["videoId"]
                    cached += 1
                    if entry.get("fallback"):
                        self.reasons[sid] = f"aproximada ({entry.get('score', 0):.2f})"
                    else:
                        self.reasons[sid] = "ya en caché"
                elif entry and entry.get("status") == "unmatched" and not self.retry_unmatched:
                    out[sid] = None
                    cached += 1
                    self.reasons[sid] = "buscada antes sin resultado"
                else:
                    todo.append(track)
        run = []
        skipped_here = 0
        for track in todo:
            with self.lock:
                if self.budget is not None and self.searched + len(run) >= self.budget:
                    out[track.sid] = None
                    self.reasons[track.sid] = "aplazada (--limit)"
                    self.limit_skipped += 1
                    skipped_here += 1
                    continue
            run.append(track)
        if run:
            extra = f" ({cached} ya resueltas antes)" if cached else ""
            log.info("  🔍 buscando %d de %d canciones en YouTube Music…%s", len(run), len(unique), extra)
            self._bar_base = (self.matched, self.unmatched, self.errors, self.approx)
            self.bar = ProgressBar(len(run), prefix="  🔍 Buscando")
            try:
                if self.workers == 1:
                    for track in run:
                        self._search_one(track, out, len(run))
                else:
                    with ThreadPoolExecutor(max_workers=self.workers) as pool:
                        list(pool.map(lambda t: self._search_one(t, out, len(run)), run))
            finally:
                b0, u0, e0, a0 = self._bar_base
                extra = f" · ≈ {self.approx - a0} aproximadas" if self.approx - a0 else ""
                if self.errors - e0:
                    extra += f" · ⚠ {self.errors - e0} errores"
                self.bar.close(suffix=f"✔ {self.matched - b0} nuevas · ✖ {self.unmatched - u0} sin match{extra}")
                self.bar = None
        elif unique and not skipped_here:
            log.info("  ✓ %d canciones, todas ya resueltas antes (0 búsquedas nuevas)", len(unique))
        return out

    def _search_one(self, track: Track, out: dict, total: int) -> None:
        try:
            video_id, score, best, best_id = best_match(self._yt(), track, self.threshold)
            err = None
        except Exception as exc:  # noqa: BLE001
            video_id, score, best, best_id, err = None, 0.0, "", None, str(exc)
        with self.lock:
            self.searched += 1
            self._progress += 1
            if err is not None:
                self.errors += 1
                prev = self.state["tracks"].get(track.sid) or {}
                if prev.get("status") == "matched" and prev.get("videoId"):
                    # Error transitorio al rebuscar (--retry-unmatched): conservo la coincidencia anterior.
                    out[track.sid] = prev["videoId"]
                    self.reasons[track.sid] = f"error de búsqueda; conservo la anterior ({err})"
                else:
                    out[track.sid] = None
                    self.reasons[track.sid] = f"error: {err}"
                log.debug("      ✖ %s: %s", track.label, err)
            elif video_id:
                self.state["tracks"][track.sid] = {
                    "videoId": video_id,
                    "status": "matched",
                    "label": track.label,
                    "score": round(score, 3),
                    "ts": int(time.time()),
                }
                self.matched += 1
                out[track.sid] = video_id
                self.reasons[track.sid] = f"nueva ({score:.2f})"
            elif best_id and self.fallback_best and score >= self.fallback_min:
                # No encontré una coincidencia clara: uso la más parecida.
                self.state["tracks"][track.sid] = {
                    "videoId": best_id,
                    "status": "matched",
                    "fallback": True,
                    "label": track.label,
                    "score": round(score, 3),
                    "ts": int(time.time()),
                }
                self.approx += 1
                self.approx_info[track.sid] = best or f"({score:.2f})"
                out[track.sid] = best_id
                self.reasons[track.sid] = f"aproximada ({score:.2f})"
                log.debug("      ≈ %s → uso la más parecida: %s", track.label, best)
            else:
                self.state["tracks"][track.sid] = {
                    "videoId": None,
                    "status": "unmatched",
                    "label": track.label,
                    "score": round(score, 3),
                    "ts": int(time.time()),
                }
                self.unmatched += 1
                out[track.sid] = None
                if best:
                    self.reasons[track.sid] = f"sin coincidencia — mejor candidato: {best}"
                else:
                    self.reasons[track.sid] = "sin resultados en la búsqueda"
                if best:
                    log.debug("      ✖ %s → mejor candidato: %s", track.label, best)
            if self.bar is not None and self.bar.enabled:
                b0, u0, e0, a0 = self._bar_base
                suffix = f"✔{self.matched - b0} ✖{self.unmatched - u0}"
                if self.approx - a0:
                    suffix += f" ≈{self.approx - a0}"
                if self.errors - e0:
                    suffix += f" ⚠{self.errors - e0}"
                self.bar.update(1, suffix=suffix)
            elif self._progress % 25 == 0 or self._progress == total:
                log.info(
                    "     %d/%d (✔ %d nuevas, ✖ %d sin match, ⚠ %d errores)",
                    self._progress,
                    total,
                    self.matched,
                    self.unmatched,
                    self.errors,
                )


# ---------------------------------- Motor ------------------------------------


class Stats(dict):
    def bump(self, key, n=1):
        self[key] = self.get(key, 0) + n


PROTECTED_PLAYLIST_IDS = {"LM", "SE"}  # «Me gusta» y «Episodios para después»: no se tocan


def purge_yt_playlists(yt: YTMTarget, dry: bool) -> int:
    """Borra TODAS tus playlists de YouTube Music (menos las automáticas del sistema)."""
    pls = [p for p in yt.library_playlists() if p.get("owned") and p["id"] not in PROTECTED_PLAYLIST_IDS]
    if not pls:
        log.info("🧹 YouTube Music: no había playlists propias que borrar")
        return 0
    if dry:
        log.info("🧹 [dry-run] borraría %d playlists de YouTube Music:", len(pls))
        for p in pls:
            log.info("   · «%s» (%d canciones)", p["title"], p["count"])
        return 0
    deleted = 0
    for p in pls:
        try:
            yt.delete_playlist(p["id"])
            deleted += 1
            log.info("   🗑 borrada «%s» (%d canciones)", p["title"], p["count"])
        except Exception as exc:  # noqa: BLE001
            log.warning("   ⚠ no pude borrar «%s»: %s", p["title"], exc)
    yt.refresh_library()
    return deleted


def check_cover_permission(yt: YTMTarget, covers_dir: Path) -> None:
    """Comprueba de verdad si YouTube permite poner portada (playlist temporal).

    No toca ninguna playlist real: crea una privada, intenta ponerle la portada
    y la borra. Sirve para abortar ANTES de un --fresh si las portadas van a fallar.
    """
    cover = next(iter(sorted(Path(covers_dir).glob("*.jpg"))), None)
    if cover is None:
        cover, _ = make_liked_cover(Path(covers_dir))
    if cover is None:
        raise SyncError("No hay ninguna imagen para comprobar las portadas")

    temp_id = None
    try:
        temp_id = yt.create_playlist("__ytsync_comprobacion_portada__", "", "PRIVATE")
        blob = upload_playlist_image(yt.yt, Path(cover))
        body = {
            "playlistId": temp_id,
            "actions": [
                {
                    "action": "ACTION_SET_CUSTOM_THUMBNAIL",
                    "addedCustomThumbnail": {
                        "imageKey": {
                            "type": "PLAYLIST_IMAGE_TYPE_CUSTOM_THUMBNAIL",
                            "name": "studio_square_thumbnail",
                        },
                        "playlistScottyEncryptedBlobId": blob,
                    },
                }
            ],
        }
        res = yt.call("comprobar portada", yt.yt._send_request, "browse/edit_playlist", body)
        if not _status_ok(res):
            raise SyncError(f"YouTube rechazó la portada: {_short_resp(res)}")
    except Exception as exc:  # noqa: BLE001
        low = str(exc).lower()
        if "403" in low or "permission" in low:
            raise SyncError(
                "YouTube no permite poner portadas en esta cuenta (403). "
                "Verifica el canal en https://www.youtube.com/verify, ejecuta `ytsync setup --refresh` "
                "para renovar la sesión y vuelve a intentarlo. No se borró nada."
            ) from exc
        raise
    finally:
        if temp_id:
            try:
                yt.delete_playlist(temp_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("   ⚠ no pude borrar la playlist temporal de prueba: %s", _short_resp(exc))


def ensure_target(pl: dict, yt: YTMTarget, state: dict, args, dry: bool):
    """Devuelve (yt_playlist_id | None, creada, propia)."""
    fresh = getattr(args, "fresh", False)
    if fresh:
        # Modo --fresh: olvido el mapeo anterior y borro las playlists viejas con el mismo nombre.
        state["playlists"].pop(pl["id"], None)
    else:
        entry = state["playlists"].get(pl["id"])
        if entry and entry.get("ytId"):
            return entry["ytId"], False, bool(entry.get("created", True))
    name = pl["name"][:YT_PLAYLIST_NAME_MAX]
    if args.reuse_by_title and not fresh:
        used = {e.get("ytId") for e in state["playlists"].values() if e.get("ytId")}
        match = yt.find_library_playlist(name, used)
        if match:
            log.info("   ♻ reutilizo la playlist existente «%s»", name)
            if not dry:
                state["playlists"].setdefault(pl["id"], {}).update(
                    {"ytId": match["id"], "created": False, "name": name}
                )
            return match["id"], False, False
    if fresh:
        used = {e.get("ytId") for e in state["playlists"].values() if e.get("ytId")}
        while True:
            match = yt.find_library_playlist(name, used)
            if not match:
                break
            if dry:
                log.info("   [dry-run] borraría la playlist vieja «%s» y la crearía de nuevo", name)
                break
            try:
                yt.delete_playlist(match["id"])
            except Exception as exc:  # noqa: BLE001
                raise SyncError(
                    f"No pude borrar la playlist vieja «{name}» ({_short_resp(exc)}); "
                    "no creo otra para no dejar duplicados"
                ) from exc
            log.info("   🗑 borré la playlist vieja «%s» (se recreará)", name)
    if dry:
        return None, False, False
    yt_id = yt.create_playlist(name, (pl.get("description") or "")[:YT_DESC_MAX], args.privacy)
    state["playlists"][pl["id"]] = {"ytId": yt_id, "created": True, "name": name}
    log.info("   ✚ playlist creada en YouTube Music")
    return yt_id, True, True


def _missing_playlist_error(exc: Exception) -> bool:
    low = str(exc).lower()
    # Solo si el error apunta a la playlist: «Video is no longer available» es una canción
    # retirada, no la playlist, y no debe tirar un mapeo sano.
    return bool(re.search(r"\b404\b", low)) or ("not found" in low and "playlist" in low)


def sync_metadata(pl: dict, yt: YTMTarget, state: dict, yt_id: str, args, covers_dir: Path, created: bool, dry: bool) -> None:
    """Iguala nombre, descripción y portada respecto a Spotify."""
    entry = state["playlists"].setdefault(pl["id"], {"ytId": yt_id})
    entry["ytId"] = yt_id
    name = pl["name"][:YT_PLAYLIST_NAME_MAX]
    desc = (pl.get("description") or "").strip()[:YT_DESC_MAX]
    cover_path, cover_sha = None, entry.get("coverSha")
    want_cover = False
    if args.covers:
        urls = pl.get("image_urls") or []
        already = entry.get("coverUrl")
        if dry:
            # En dry-run no descargamos nada: solo anotamos si la portada cambiaría.
            want_cover = bool(urls) and not (already and entry.get("coverSha") and already in urls)
        elif not (already and entry.get("coverSha") and already in urls):
            path, sha, used_url = fetch_cover(urls, covers_dir, liked_fallback=bool(pl.get("liked")))
            if path and sha:
                if used_url:
                    entry["coverUrl"] = used_url
                if sha != entry.get("coverSha"):
                    cover_path, cover_sha = path, sha
    want_name = created or name != entry.get("name")
    want_desc = created or desc != entry.get("desc")
    if not (want_name or want_desc or cover_path or want_cover):
        return
    if dry:
        what = ", ".join(
            x
            for x in (
                "nombre" if want_name else "",
                "descripción" if want_desc else "",
                "portada" if (cover_path or want_cover) else "",
            )
            if x
        )
        log.info("   [dry-run] actualizaría %s", what)
        return
    cover_failed = False
    try:
        yt.edit_metadata(
            yt_id,
            title=name if want_name else None,
            description=desc if want_desc else None,
            image_path=cover_path,
        )
    except Exception as exc:  # noqa: BLE001
        if cover_path is None:
            raise
        cover_failed = True
        low = str(exc).lower()
        if "403" in low or "permission" in low:
            log.warning(
                "   ⚠ YouTube rechazó la portada (403): el canal no está verificado o la sesión es antigua.\n"
                "     Verifica el canal en https://www.youtube.com/verify y luego ejecuta `ytsync setup --refresh`."
            )
        else:
            log.warning("   ⚠ no pude aplicar la portada (%s); lo reintentaré", str(exc)[:300])
        entry.pop("coverUrl", None)  # para volver a intentarlo en la próxima ejecución
        yt.edit_metadata(
            yt_id,
            title=name if want_name else None,
            description=desc if want_desc else None,
            image_path=None,
        )
        cover_sha = None
    entry.update({"name": name, "desc": desc})
    if cover_sha:
        entry["coverSha"] = cover_sha
    what = ", ".join(
        x
        for x in (
            "nombre" if want_name else "",
            "descripción" if want_desc else "",
            "portada" if (cover_path or want_cover) and not cover_failed else "",
        )
        if x
    )
    if what:
        log.info("   🎨 metadatos actualizados (%s)", what)


def compute_moves(current: list, desired: list) -> list:
    """Movimientos para pasar de `current` a `desired` (mismo multiconjunto de videoIds)."""
    cur = [dict(item) for item in current]
    moves = []
    for i, want in enumerate(desired):
        if i >= len(cur):
            break
        if cur[i]["videoId"] == want:
            continue
        j = next((k for k in range(i + 1, len(cur)) if cur[k]["videoId"] == want), None)
        if j is None:
            continue
        item = cur.pop(j)
        moves.append((item["setVideoId"], cur[i]["setVideoId"]))
        cur.insert(i, item)
    return moves


def _present_order(current: list, desired: list) -> list:
    """videoIds de `current` que aún se esperan, en su orden actual (respetando duplicados)."""
    remaining = Counter(desired)
    out = []
    for item in current:
        video_id = item["videoId"]
        if remaining[video_id] > 0:
            out.append(video_id)
            remaining[video_id] -= 1
    return out


def _desired_order(current: list, desired: list) -> list:
    """videoIds de `desired` que están presentes, en el orden deseado (respetando duplicados)."""
    remaining = Counter(item["videoId"] for item in current)
    out = []
    for video_id in desired:
        if remaining[video_id] > 0:
            out.append(video_id)
            remaining[video_id] -= 1
    return out


def _order_ok(current: list, desired: list) -> bool:
    """True si las canciones presentes ya están en el orden relativo de Spotify.

    No exige que estén todas: si YouTube no tiene alguna, ordena las demás.
    """
    return _present_order(current, desired) == _desired_order(current, desired)


def apply_tracks(yt: YTMTarget, yt_id: str, desired: list, args, dry: bool, skip_fetch: bool = False) -> dict:
    current = [] if skip_fetch else yt.get_items(yt_id)
    cur_cnt, des_cnt = Counter(c["videoId"] for c in current), Counter(desired)

    # 1) Bajas: lo que ya no está en Spotify.
    remove_items = []
    if args.prune and desired and current:
        budget = cur_cnt - des_cnt
        for item in reversed(current):
            if budget[item["videoId"]] > 0:
                remove_items.append(item)
                budget[item["videoId"]] -= 1
        if remove_items and len(remove_items) > max(10, int(0.5 * len(current))) and not args.force_prune:
            log.warning(
                "   ⚠ se eliminarían %d de %d canciones; omito el borrado (usa --force-prune si es correcto)",
                len(remove_items),
                len(current),
            )
            remove_items = []

    # 2) Altas: lo que falta (respetando duplicados).
    normal, dups, seen = [], [], set()
    for video_id in desired:
        if video_id in seen:
            continue
        seen.add(video_id)
        missing = des_cnt[video_id] - cur_cnt.get(video_id, 0)
        if missing <= 0:
            continue
        if cur_cnt.get(video_id, 0) == 0:
            normal.append(video_id)
            missing -= 1
        dups.extend([video_id] * max(0, missing))
    added = len(normal) + len(dups)

    if dry:
        sim = [c for c in current if c not in remove_items]
        sim += [{"videoId": v, "setVideoId": f"NEW{i}"} for i, v in enumerate(normal + dups)]
        moved = 0
        if args.order and Counter(c["videoId"] for c in sim) == des_cnt:
            moved = len(compute_moves(sim, desired))
        return {"added": added, "removed": len(remove_items), "moved": moved}

    if remove_items:
        log.info("   🗑 quitando %d canciones que ya no están en Spotify", len(remove_items))
        yt.remove_items(yt_id, remove_items)
    to_add = normal + dups
    if to_add:
        yt.add_items(yt_id, to_add, duplicates=bool(dups))
        if dups:
            log.info("   ℹ %d de las añadidas son copias duplicadas que también están en Spotify", len(dups))

    def _settle(tries=8, delay=0.8):
        """Relee hasta que el orden relativo de las canciones presentes sea el deseado."""
        items = yt.get_items(yt_id)
        for _ in range(tries):
            if _order_ok(items, desired):
                return items
            time.sleep(delay)
            items = yt.get_items(yt_id)
        return items

    moved = 0
    if args.order and desired:
        if remove_items or added:
            current = yt.get_items(yt_id)
            for _ in range(8):  # tras añadir, esperar a que aparezca todo
                if Counter(c["videoId"] for c in current) == des_cnt:
                    break
                time.sleep(0.8)
                current = yt.get_items(yt_id)
        for round_no in range(1, 4):
            moves = compute_moves(current, desired)
            if not moves:
                break
            if round_no == 1:
                log.info("   ↷ reordenando %d canciones para dejarlas igual que en Spotify", len(moves))
            yt.move_items(yt_id, moves)
            moved += len(moves)
            current = _settle()
            if _order_ok(current, desired):
                break
        now_cnt = Counter(c["videoId"] for c in current)
        missing = sum((des_cnt - now_cnt).values())
        extra = sum((now_cnt - des_cnt).values())
        if missing:
            log.warning(
                "   ⚠ faltan %d canciones en YouTube Music (no se pudieron añadir); ordené las presentes",
                missing,
            )
        if extra and args.prune:
            log.warning("   ⚠ quedan %d canciones de más en YouTube Music; no pude quitarlas", extra)
        if not _order_ok(current, desired):
            log.warning("   ⚠ no pude dejar el orden idéntico al de Spotify (revisa esa playlist a mano)")
    return {"added": added, "removed": len(remove_items), "moved": moved}


def sync_one_playlist(pl, sp, yt, state, resolver, args, dry, covers_dir, stats: Stats, report: list, approx: list, persist) -> None:
    tracks = sp.playlist_tracks(pl["id"])
    if not tracks:
        log.info("   (vacía o sin canciones accesibles, se omite)")
        return
    log.info("   🎵 %d canciones en la playlist", len(tracks))
    resolved = resolver.resolve(tracks)
    desired = [resolved[t.sid] for t in tracks if resolved.get(t.sid)]
    for track in tracks:
        if not resolved.get(track.sid):
            report.append(f"[{pl['name']}] {track.label} — {resolver.reasons.get(track.sid, '?')}")
        elif resolver.approx_info.get(track.sid):
            approx.append(f"[{pl['name']}] {track.label} → {resolver.approx_info[track.sid]}")
    stats.bump("unmatched", len(tracks) - len(desired))
    yt_id, created, owned = ensure_target(pl, yt, state, args, dry)
    if not yt_id:
        log.info("   [dry-run] crearía la playlist y añadiría %d de %d canciones", len(desired), len(tracks))
        stats.bump("created")
        return
    if created or owned or args.update_metadata:
        sync_metadata(pl, yt, state, yt_id, args, covers_dir, created, dry)
    result = apply_tracks(yt, yt_id, desired, args, dry, skip_fetch=created)
    stats.bump("added", result["added"])
    stats.bump("removed", result["removed"])
    stats.bump("moved", result["moved"])
    if created:
        stats.bump("created")
    else:
        stats.bump("playlists_ok")
    extra = f" −{result['removed']}" if result["removed"] else ""
    extra += f" ↷{result['moved']}" if result["moved"] else ""
    log.info(
        "   ✅ +%d%s (%d deseadas, %d sin match)%s",
        result["added"],
        extra,
        len(desired),
        len(tracks) - len(desired),
        " [dry-run]" if dry else "",
    )
    persist()


def sync_liked(sp, yt, state, args, resolver, dry, covers_dir, stats: Stats, report: list, approx: list, persist, me) -> None:
    tracks = sp.liked_tracks()
    log.info("❤ Me gusta de Spotify: %d canciones", len(tracks))
    if not tracks:
        return
    resolved = resolver.resolve(tracks)
    desired = [resolved[t.sid] for t in tracks if resolved.get(t.sid)]
    for track in tracks:
        if not resolved.get(track.sid):
            report.append(f"[Me gusta] {track.label} — {resolver.reasons.get(track.sid, '?')}")
        elif resolver.approx_info.get(track.sid):
            approx.append(f"[Me gusta] {track.label} → {resolver.approx_info[track.sid]}")
    stats.bump("unmatched", len(tracks) - len(desired))

    if args.liked_mode in ("playlist", "both"):
        meta = {
            "id": "__liked__",
            "name": args.liked_playlist_name,
            "description": "",
            "owner_id": (me or {}).get("id") or "",
            "image_urls": LIKED_COVER_URLS,
            "liked": True,
            "total": len(tracks),
        }
        yt_id, created, owned = ensure_target(meta, yt, state, args, dry)
        if not yt_id:
            log.info("   [dry-run] crearía «%s» con %d canciones", meta["name"], len(desired))
            stats.bump("created")
        else:
            if created or owned or args.update_metadata:
                sync_metadata(meta, yt, state, yt_id, args, covers_dir, created, dry)
            result = apply_tracks(yt, yt_id, desired, args, dry, skip_fetch=created)
            stats.bump("added", result["added"])
            stats.bump("removed", result["removed"])
            stats.bump("moved", result["moved"])
            if created:
                stats.bump("created")
            else:
                stats.bump("playlists_ok")
            log.info(
                "   ✅ playlist «%s»: +%d −%d ↷%d%s",
                meta["name"],
                result["added"],
                result["removed"],
                result["moved"],
                " [dry-run]" if dry else "",
            )
            persist()

    if args.liked_mode in ("like", "both"):
        done = state.setdefault("liked", {}).setdefault("done", {})
        current = {t.sid for t in tracks}
        todo = [(t, resolved[t.sid]) for t in tracks if resolved.get(t.sid) and done.get(t.sid) != resolved[t.sid]]
        if todo:
            if dry:
                log.info("   [dry-run] marcaría Me gusta en YouTube Music a %d canciones", len(todo))
            else:
                log.info("   👍 marcando Me gusta en YouTube Music (%d canciones)…", len(todo))
                bar = ProgressBar(len(todo), prefix="  👍 Me gusta")
                ok = 0
                try:
                    for idx, (track, video_id) in enumerate(todo, 1):
                        try:
                            yt.rate(video_id, like=True)
                            done[track.sid] = video_id
                            ok += 1
                        except Exception as exc:  # noqa: BLE001
                            log.debug("      ✖ no pude marcar %s: %s", track.label, exc)
                        if bar.enabled:
                            bar.update(1, suffix=f"✔ {ok}")
                        elif idx % 25 == 0 or idx == len(todo):
                            log.info("     %d/%d", idx, len(todo))
                finally:
                    bar.close(suffix=f"✔ {ok}/{len(todo)}")
                stats.bump("liked", ok)
                log.info("   ✅ Me gusta nuevos: %d/%d", ok, len(todo))
        if args.prune:
            stale = [(sid, vid) for sid, vid in list(done.items()) if sid not in current]
            if stale:
                if dry:
                    log.info("   [dry-run] retiraría %d Me gusta que ya no están en Spotify", len(stale))
                else:
                    log.info("   👎 retirando %d Me gusta que ya no están en Spotify…", len(stale))
                    bar = ProgressBar(len(stale), prefix="  👎 Retirando")
                    try:
                        for sid, video_id in stale:
                            try:
                                yt.rate(video_id, like=False)
                                done.pop(sid, None)
                                stats.bump("unliked")
                            except Exception as exc:  # noqa: BLE001
                                log.debug("      ✖ no pude retirar un Me gusta: %s", exc)
                            bar.update(1)
                    finally:
                        bar.close(suffix=f"{len(stale)} retirados")
        persist()


# ---------------------------------- Comandos ---------------------------------


def playlist_selected(pl: dict, args) -> bool:
    name = pl["name"].casefold()
    if args.only and not any(o.casefold() in name for o in args.only):
        return False
    if args.exclude and any(e.casefold() in name for e in args.exclude):
        return False
    return True


def write_report(
    adir: Path,
    lines: list,
    dry: bool,
    name: str = "unmatched.txt",
    title: str = "Canciones de Spotify que no encontré en YouTube Music",
):
    if dry:
        return None
    path = adir / name
    header = f"{title}\nGenerado: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    body = ("\n".join(lines) + "\n") if lines else "¡Ninguna! 🎉\n"
    path.write_text(header + body, encoding="utf-8")
    return path


def _show_list(title: str, lines: list, path, dry: bool, max_show: int = 20) -> None:
    """Muestra al final un resumen de pendientes/aproximadas con su archivo completo."""
    if not lines:
        return
    log.info("")
    log.info("  ── %s: %d ──", title, len(lines))
    for line in lines[:max_show]:
        log.info("     · %s", line)
    if len(lines) > max_show:
        log.info("     … y %d más", len(lines) - max_show)
    if dry:
        log.info("     (dry-run) no se escribe el archivo")
    elif path:
        log.info("     📄 Lista completa: %s", path)


def cmd_sync(args, cfg) -> int:
    adir = cfg["app_dir"]
    dry = args.dry_run
    if not dry:
        adir.mkdir(parents=True, exist_ok=True)
        (adir / "covers").mkdir(exist_ok=True)
    covers_dir = adir / "covers"
    state_path = adir / "state.json"
    state = load_state(state_path)
    pace = Pacer(args.delay)

    def persist():
        if not dry:
            save_state(state_path, state)

    sp = SpotifySource(cfg, pace, adir / ".spotify_token")
    me = sp.me()
    log.info("👤 Spotify: %s", me.get("display_name") or me.get("id"))

    yt_auth = Path(args.yt_auth).expanduser() if args.yt_auth else adir / "ytmusic_headers.json"
    yt = YTMTarget(yt_auth, pace)
    log.info("👤 YouTube Music: %s", yt.account_label())
    if dry:
        log.info("🧪 MODO PRUEBA (--dry-run): no se escribirá nada")

    make_yt = lambda: YTMTarget(yt_auth, Pacer(args.delay))  # noqa: E731
    resolver = Resolver(
        make_yt,
        state,
        args.threshold,
        args.retry_unmatched,
        args.limit,
        args.workers,
        fallback_best=args.fallback_best,
        fallback_min=args.fallback_min,
    )
    stats = Stats()
    report: list = []
    approx_report: list = []

    if (
        args.covers
        and not dry
        and (
            getattr(args, "require_covers", False)
            or (args.fresh and not getattr(args, "no_cover_check", False))
        )
    ):
        log.info("🖼 comprobando que YouTube permite poner portadas…")
        check_cover_permission(yt, covers_dir)
        log.info("   ✓ portadas permitidas")

    if args.fresh:
        if args.only or args.exclude or args.skip_playlists:
            log.warning(
                "🧹 --fresh con filtros: solo borro las playlists que coincidan por nombre (no todo YouTube Music)"
            )
        else:
            deleted = purge_yt_playlists(yt, dry)
            stats.bump("purged", deleted)
            if deleted and not dry:
                state["playlists"].clear()
                persist()

    try:
        if args.liked_mode != "skip":
            sync_liked(sp, yt, state, args, resolver, dry, covers_dir, stats, report, approx_report, persist, me)
        if not args.skip_playlists:
            all_pls = sp.playlists()
            selected = []
            for pl in all_pls:
                if pl["owner_id"] == "spotify" and not args.include_editorial:
                    log.debug("   · se omite playlist de Spotify: «%s»", pl["name"])
                    continue
                if args.skip_followed and pl["owner_id"] != (me.get("id") or ""):
                    continue
                if not playlist_selected(pl, args):
                    continue
                selected.append(pl)
            log.info("🎧 Playlists a sincronizar: %d de %d", len(selected), len(all_pls))
            for i, pl in enumerate(selected, 1):
                log.info("▶ [%d/%d] «%s»", i, len(selected), pl["name"])
                try:
                    sync_one_playlist(pl, sp, yt, state, resolver, args, dry, covers_dir, stats, report, approx_report, persist)
                except Exception as exc:  # noqa: BLE001
                    log.error("   ✖ error en «%s»: %s", pl["name"], exc)
                    stats.bump("errors")
                    report.append(f"[{pl['name']}] ERROR: {exc}")
                    if not dry and _missing_playlist_error(exc):
                        state["playlists"].pop(pl["id"], None)
                        log.warning(
                            "   ↻ la playlist guardada ya no existe en YouTube Music; se recreará en la próxima ejecución"
                        )
                    persist()
    except KeyboardInterrupt:
        log.warning("⏹ Interrumpido por el usuario; guardando estado…")
    finally:
        persist()

    log.info("")
    log.info("────────────── Resumen ──────────────")
    ok = stats.get("playlists_ok", 0)
    created = stats.get("created", 0)
    log.info("  Playlists procesadas: %d (creadas: %d, actualizadas: %d)", ok + created, created, ok)
    log.info(
        "  Canciones añadidas: %s | eliminadas: %s | movidas: %s",
        stats.get("added", 0),
        stats.get("removed", 0),
        stats.get("moved", 0),
    )
    if args.fresh:
        log.info("  Playlists borradas en YouTube: %s", stats.get("purged", 0))
    log.info("  Me gusta nuevos: %s | retirados: %s", stats.get("liked", 0), stats.get("unliked", 0))
    log.info(
        "  Búsquedas nuevas: %s (✔ %s, ✖ %s, ≈ %s, ⚠ %s)",
        resolver.searched,
        resolver.matched,
        resolver.unmatched,
        resolver.approx,
        resolver.errors,
    )
    if resolver.limit_skipped:
        log.info("  Aplazadas por --limit: %s (vuelve a ejecutar para continuar)", resolver.limit_skipped)
    if stats.get("unmatched"):
        log.info("  ⚠ Canciones sin coincidencia: %s", stats.get("unmatched"))
    if resolver.approx:
        log.info("  ≈ Aproximadas (no encontré la original; puse la más parecida): %s", resolver.approx)
    if stats.get("errors"):
        log.info("  ⚠ Errores: %s", stats.get("errors"))
    path_bad = write_report(adir, report, dry)
    path_approx = write_report(
        adir,
        approx_report,
        dry,
        name="approximate.txt",
        title=(
            "No encontré estas canciones en YouTube Music: puse la más parecida.\n"
            "Revisa si te convence; para reintentar la búsqueda usa --retry-unmatched"
        ),
    )
    _show_list("❌ No encontradas (revisa a mano)", report, path_bad, dry)
    _show_list("≈ Aproximadas usadas", approx_report, path_approx, dry)
    if dry:
        log.info("  🧪 Dry-run: no se cambió nada. Ejecuta sin --dry-run para aplicar.")
    log.info("─────────────────────────────────────")
    return 1 if stats.get("errors") else 0


CONFIG_EXAMPLE = """\
# Credenciales de tu app de Spotify (https://developer.spotify.com/dashboard)
SPOTIFY_CLIENT_ID=
SPOTIFY_CLIENT_SECRET=
# Debe coincidir EXACTAMENTE con el «Redirect URI» que configures en la app
SPOTIFY_REDIRECT_URI=http://127.0.0.1:8888/callback
"""


def cmd_setup(args, cfg) -> int:
    adir = cfg["app_dir"]
    adir.mkdir(parents=True, exist_ok=True)
    example = Path.cwd() / "config.env.example"
    if not example.exists():
        example.write_text(CONFIG_EXAMPLE, encoding="utf-8")

    missing = [k for k in ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET") if not cfg.get(k)]
    if missing:
        log.error("Faltan credenciales de Spotify: %s", ", ".join(missing))
        log.error("")
        log.error("1) Entra en https://developer.spotify.com/dashboard y crea una app (nombre libre).")
        log.error("2) En la app, añade este «Redirect URI» exacto: %s", cfg.get("SPOTIFY_REDIRECT_URI"))
        log.error("3) Copia Client ID y Client Secret a un archivo config.env:")
        log.error("")
        log.error("     SPOTIFY_CLIENT_ID=tu_id")
        log.error("     SPOTIFY_CLIENT_SECRET=tu_secret")
        log.error("     SPOTIFY_REDIRECT_URI=%s", cfg.get("SPOTIFY_REDIRECT_URI"))
        log.error("")
        log.error("Plantilla dejada en: %s", example)
        return 2

    log.info("· Conectando con Spotify (la primera vez se abre el navegador)…")
    sp = SpotifySource(cfg, Pacer(0.2), adir / ".spotify_token")
    me = sp.me()
    log.info("✅ Spotify conectado como: %s", me.get("display_name") or me.get("id"))

    yt_auth = Path(args.yt_auth).expanduser() if args.yt_auth else adir / "ytmusic_headers.json"
    refresh = bool(getattr(args, "refresh", False))
    if yt_auth.is_file() and not refresh:
        try:
            yt = YTMTarget(yt_auth, Pacer(0.2))
            log.info("✅ YouTube Music conectado como: %s", yt.account_label())
            log.info("   (si quieres renovar la sesión, ejecuta `setup --refresh`)")
            return 0
        except Exception as exc:  # noqa: BLE001
            log.warning("⚠ La sesión guardada de YouTube Music no funciona (%s); la renovamos.", exc)

    backup = None
    if yt_auth.is_file():
        backup = yt_auth.with_name(yt_auth.name + ".bak")
        try:
            backup.write_bytes(yt_auth.read_bytes())
            os.chmod(backup, 0o600)
        except OSError:
            backup = None

    def restore_backup() -> None:
        """Devuelve la sesión anterior y borra la copia con cookies."""
        if not backup:
            return
        try:
            yt_auth.write_bytes(backup.read_bytes())
            os.chmod(yt_auth, 0o600)
            log.warning("   ↺ restauré la sesión anterior")
        except OSError:
            pass
        try:
            backup.unlink()
        except OSError:
            pass

    log.info("")
    log.info("· Conectando con YouTube Music")
    if refresh:
        log.info("  ↻ Renovando la sesión aunque la anterior todavía funcione.")
    log.info("  1. Abre https://music.youtube.com en el navegador (con tu cuenta) y recarga la página.")
    log.info("  2. Abre las herramientas de desarrollador (F12) → pestaña «Red / Network».")
    log.info("  3. Filtra por «browse» y haz clic en cualquier canción para que aparezca una petición POST.")
    log.info("  4. Clic derecho sobre esa petición → Copiar → «Copiar encabezados de solicitud».")
    log.info("  5. Pega todo a continuación y pulsa Ctrl-D (no Enter) para terminar.")
    log.info("")
    try:
        from ytmusicapi import setup as yt_setup
    except ImportError:
        log.error("Tu ytmusicapi es demasiado antiguo; actualízalo con: pip install -U ytmusicapi")
        return 2
    try:
        yt_setup(filepath=str(yt_auth))
    except KeyboardInterrupt:
        restore_backup()
        log.error("Cancelado.")
        return 130
    except Exception as exc:  # noqa: BLE001
        restore_backup()
        log.error("No se pudo guardar la sesión de YouTube Music: %s", exc)
        return 2
    try:
        os.chmod(yt_auth, 0o600)
    except OSError:
        pass
    try:
        yt = YTMTarget(yt_auth, Pacer(0.2))
        label = yt.account_label()
    except Exception as exc:  # noqa: BLE001
        log.error("La sesión nueva de YouTube Music no funciona: %s", exc)
        restore_backup()
        return 2
    if backup:
        try:
            backup.unlink()
        except OSError:
            pass
    log.info("✅ YouTube Music conectado como: %s", label)
    log.info("")
    log.info("Todo listo 🎉  Prueba primero:  .venv/bin/python sync.py sync --dry-run")
    return 0


def cmd_status(args, cfg) -> int:
    adir = cfg["app_dir"]
    state = load_state(adir / "state.json")
    tracks = state.get("tracks") or {}
    matched = sum(1 for e in tracks.values() if e.get("status") == "matched")
    unmatched = sum(1 for e in tracks.values() if e.get("status") == "unmatched")
    log.info("Configuración:   %s", adir)
    log.info("Estado:          %s", adir / "state.json")
    log.info("Sesión YT Music: %s", adir / "ytmusic_headers.json")
    log.info("")
    log.info("Canciones en caché:   %d (✔ %d con match, ✖ %d sin match)", len(tracks), matched, unmatched)
    log.info("Playlists mapeadas:   %d", len(state.get("playlists") or {}))
    log.info("Me gusta registrados: %d", len((state.get("liked") or {}).get("done") or {}))
    if state.get("last_run"):
        log.info("Última ejecución:     %s", state["last_run"])
    return 0


# ----------------------------------- CLI -------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true", help="muestra detalles de depuración")
    common.add_argument("--config", help="ruta a config.env (por defecto: ./config.env o el directorio de la app)")
    common.add_argument("--yt-auth", help="ruta al JSON de sesión de YouTube Music")

    parser = argparse.ArgumentParser(
        prog="sync.py",
        description="Sincroniza Spotify ➜ YouTube Music (Me gusta y playlists, con portada, descripción y orden).",
    )
    sub = parser.add_subparsers(dest="cmd")

    setup_p = sub.add_parser("setup", parents=[common], help="conecta Spotify y YouTube Music")
    setup_p.add_argument(
        "--refresh",
        action="store_true",
        help="vuelve a pedir las cabeceras de YouTube Music aunque la sesión guardada todavía funcione",
    )

    p = sub.add_parser("sync", parents=[common], help="sincroniza Me gusta y playlists")
    p.add_argument("--dry-run", action="store_true", help="no cambia nada; solo muestra qué haría")
    p.add_argument(
        "--liked-mode",
        choices=["like", "playlist", "both", "skip"],
        default="like",
        help="Me gusta: like (dar Me gusta en la lista oficial de YT; por defecto), playlist (crear playlist idéntica), both o skip",
    )
    p.add_argument("--liked-playlist-name", default="Liked Songs (Spotify)", help="nombre de la playlist de Me gusta")
    p.add_argument("--privacy", choices=["PRIVATE", "UNLISTED", "PUBLIC"], default="PRIVATE", help="visibilidad de playlists nuevas")
    p.add_argument("--only", action="append", default=[], metavar="TEXTO", help="solo playlists cuyo nombre contenga TEXTO (repetible)")
    p.add_argument("--exclude", action="append", default=[], metavar="TEXTO", help="excluye playlists cuyo nombre contenga TEXTO (repetible)")
    p.add_argument("--skip-playlists", action="store_true", help="no tocar playlists (solo Me gusta)")
    p.add_argument("--skip-followed", action="store_true", help="ignora playlists de otros que sigues")
    p.add_argument("--include-editorial", action="store_true", help="incluye playlists de Spotify (Discover Weekly, mixes…)")
    p.add_argument("--no-prune", dest="prune", action="store_false", help="no borra en YT lo que ya no está en Spotify")
    p.add_argument("--force-prune", action="store_true", help="permite borrados masivos sin la salvaguarda del 50%%")
    p.add_argument("--no-covers", dest="covers", action="store_false", help="no copia las portadas")
    p.add_argument("--no-order", dest="order", action="store_false", help="no corrige el orden de las canciones")
    p.add_argument("--update-metadata", action="store_true", help="actualiza también nombre/descripción/portada de playlists existentes reutilizadas")
    p.add_argument("--no-reuse-by-title", dest="reuse_by_title", action="store_false", help="crea playlists nuevas aunque exista otra con el mismo nombre")
    p.add_argument(
        "--fresh",
        action="store_true",
        help="🔴 borra TODAS tus playlists de YouTube Music (excepto «Me gusta» y listas del sistema) y las clona de nuevo desde cero",
    )
    p.add_argument(
        "--require-covers",
        action="store_true",
        help="comprueba que YouTube permite poner portadas antes de sincronizar; si no, aborta sin borrar nada",
    )
    p.add_argument(
        "--no-cover-check",
        action="store_true",
        help="no compruebes las portadas antes de --fresh (si fallan, las playlists se quedan sin portada)",
    )
    p.add_argument("--threshold", type=float, default=0.65, help="umbral de coincidencia 0-1 (por defecto 0.65)")
    p.add_argument(
        "--no-fallback-best",
        dest="fallback_best",
        action="store_false",
        help="si no encuentra la canción, NO pone la más parecida (por defecto sí la pone)",
    )
    p.add_argument(
        "--fallback-min",
        type=float,
        default=0.30,
        metavar="0-1",
        help="puntuación mínima para aceptar «la más parecida» (por defecto 0.30)",
    )
    p.add_argument("--workers", type=int, default=2, help="búsquedas en paralelo (por defecto 2)")
    p.add_argument("--delay", type=float, default=0.35, help="segundos entre peticiones (por defecto 0.35)")
    p.add_argument("--limit", type=int, default=0, help="máx. de búsquedas nuevas por ejecución (0 = sin límite)")
    p.add_argument(
        "--retry-unmatched",
        action="store_true",
        help="vuelve a buscar las sin coincidencia y las aproximadas (por si ya están en YT Music)",
    )

    sub.add_parser("status", parents=[common], help="resumen del estado guardado")
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return 1
    if getattr(args, "limit", 0) and args.limit < 0:
        parser.error("--limit no puede ser negativo")
    setup_logging(args.verbose)
    try:
        cfg = build_config(args)
        if args.cmd == "setup":
            return cmd_setup(args, cfg)
        if args.cmd == "status":
            return cmd_status(args, cfg)
        return cmd_sync(args, cfg)
    except SyncError as exc:
        log.error("✖ %s", exc)
        return 2
    except KeyboardInterrupt:
        log.error("⏹ Cancelado.")
        return 130
    except Exception as exc:  # noqa: BLE001
        log.error("✖ error inesperado: %s", exc)
        log.debug("traza completa", exc_info=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

import hashlib
import json
import logging
import re
import sqlite3
import time
import urllib.parse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

from .models import (
    Album,
    AlbumCandidate,
    AlbumInfo,
    Artist,
    ArtistCandidate,
    ArtistInfo,
    TrackInfo,
    TrackSearchItem,
)


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    value = value.lower().strip()
    value = re.sub(r"\(.*?\)", "", value)
    value = re.sub(r"\[.*?\]", "", value)
    return re.sub(r"[^a-z0-9]", "", value)


def quality_rank(track: TrackSearchItem | TrackInfo | Dict[str, Any]) -> int:
    bit_depth = _get_field(track, "bitDepth")
    sample_rate = _get_field(track, "sampleRate")
    if bit_depth is not None and sample_rate is not None:
        try:
            if int(bit_depth) > 16 or int(sample_rate) > 44100:
                return 4
            return 3
        except (TypeError, ValueError):
            pass

    quality = (
        _get_field(track, "resolvedQuality")
        or _get_field(track, "audioQuality")
        or ""
    )
    return {
        "LOW": 1,
        "HIGH": 2,
        "LOSSLESS": 3,
        "HI_RES_LOSSLESS": 4,
    }.get(str(quality), 0)


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _track_primary_artist_name(track: TrackSearchItem | TrackInfo | Dict[str, Any]) -> str:
    artist = _get_field(track, "artist")
    artist_name = _get_field(artist, "name") if artist else ""
    if artist_name:
        return artist_name
    for entry in _get_field(track, "artists", []) or []:
        name = _get_field(entry, "name")
        if name:
            return name
    return ""


def _track_key(track: TrackSearchItem) -> Tuple[str, str, bool]:
    title_key = normalize_text(" ".join(part for part in [track.title, track.version or ""] if part))
    artist_key = normalize_text(_track_primary_artist_name(track))
    return (title_key, artist_key, bool(track.explicit))


def _relevance_score(query: str, item_title: str, item_artist: str = "") -> int:
    """Score how closely an item matches the user's original query."""
    q = normalize_text(query)
    title = normalize_text(item_title)
    artist = normalize_text(item_artist)
    score = 0
    if q == title:
        score += 100
    elif q and title and q in title:
        score += 60
    elif q and title and title in q:
        score += 40
    if q and artist and q in artist:
        score += 20
    if q and artist and artist in q:
        score += 10
    return score


def track_metadata_score(track: TrackSearchItem | TrackInfo | Dict[str, Any]) -> int:
    score = 0
    if normalize_text(_get_field(track, "title")):
        score += 4
    if normalize_text(_track_primary_artist_name(track)):
        score += 4

    album = _get_field(track, "album")
    album_title = _get_field(album, "title") if album else ""
    if normalize_text(album_title):
        score += 3

    duration = _get_field(track, "duration") or 0
    if duration > 0:
        score += 3
    if _has_value(_get_field(track, "isrc")):
        score += 2
    if _has_value(_get_field(track, "releaseDate")):
        score += 1
    if _get_field(track, "trackNumber"):
        score += 1
    if _get_field(track, "volumeNumber"):
        score += 1
    if quality_rank(track) > 0:
        score += 1
    if _get_field(track, "bitDepth"):
        score += 1
    if _get_field(track, "sampleRate"):
        score += 1
    return score


def _merge_artist(primary: Artist | None, fallback: Artist | None) -> Artist | None:
    if primary is None:
        return fallback
    if fallback is None:
        return primary
    return primary.model_copy(
        update={
            "id": primary.id if _has_value(primary.id) else fallback.id,
            "name": primary.name or fallback.name,
            "picture": primary.picture or fallback.picture,
            "type": primary.type or fallback.type,
            "area": primary.area or fallback.area,
            "disambiguation": primary.disambiguation or fallback.disambiguation,
            "provider": primary.provider or fallback.provider,
        }
    )


def _merge_artists(primary: List[Artist], fallback: List[Artist]) -> List[Artist]:
    merged: List[Artist] = []
    seen = set()
    for artist in (primary or []) + (fallback or []):
        if artist is None or not artist.name:
            continue
        key = normalize_text(artist.name)
        if key in seen:
            continue
        seen.add(key)
        merged.append(artist)
    return merged


def _merge_album(primary: Album | None, fallback: Album | None) -> Album | None:
    if primary is None:
        return fallback
    if fallback is None:
        return primary
    return primary.model_copy(
        update={
            "id": primary.id if _has_value(primary.id) else fallback.id,
            "title": primary.title or fallback.title,
            "cover": primary.cover or fallback.cover,
            "vibrantColor": primary.vibrantColor or fallback.vibrantColor,
            "artist": _merge_artist(primary.artist, fallback.artist),
            "releaseDate": primary.releaseDate or fallback.releaseDate,
            "disambiguation": primary.disambiguation or fallback.disambiguation,
            "provider": primary.provider or fallback.provider,
        }
    )


def _track_rank(track: TrackSearchItem) -> Tuple[int, int, int]:
    return (track_metadata_score(track), quality_rank(track), track.duration or 0)


def _merge_tracks(primary: TrackSearchItem, fallback: TrackSearchItem) -> TrackSearchItem:
    merged_artist = _merge_artist(primary.artist, fallback.artist)
    merged_artists = _merge_artists(primary.artists, fallback.artists)
    if not merged_artists and merged_artist is not None:
        merged_artists = [merged_artist]
    if merged_artist is None and merged_artists:
        merged_artist = merged_artists[0]

    return primary.model_copy(
        update={
            "id": primary.id if _has_value(primary.id) else fallback.id,
            "title": primary.title or fallback.title,
            "duration": primary.duration or fallback.duration or 0,
            "popularity": primary.popularity if primary.popularity is not None else fallback.popularity,
            "artist": merged_artist,
            "artists": merged_artists,
            "album": _merge_album(primary.album, fallback.album),
            "explicit": primary.explicit if primary.explicit is not None else fallback.explicit,
            "audioQuality": primary.audioQuality or fallback.audioQuality,
            "bitDepth": primary.bitDepth or fallback.bitDepth,
            "sampleRate": primary.sampleRate or fallback.sampleRate,
            "mediaMetadata": primary.mediaMetadata or fallback.mediaMetadata,
            "version": primary.version or fallback.version,
            "isrc": primary.isrc or fallback.isrc,
            "copyright": primary.copyright or fallback.copyright,
            "genre": primary.genre or fallback.genre,
            "label": primary.label or fallback.label,
            "composer": primary.composer or fallback.composer,
            "bpm": primary.bpm or fallback.bpm,
            "key": primary.key or fallback.key,
            "keyScale": primary.keyScale or fallback.keyScale,
            "releaseDate": primary.releaseDate or fallback.releaseDate,
            "trackNumber": primary.trackNumber or fallback.trackNumber,
            "volumeNumber": primary.volumeNumber or fallback.volumeNumber,
            "trackTotal": primary.trackTotal or fallback.trackTotal,
            "discTotal": primary.discTotal or fallback.discTotal,
            "streamStartDate": primary.streamStartDate or fallback.streamStartDate,
            "provider": primary.provider or fallback.provider,
        }
    )


def deduplicate_tracks(tracks: List[TrackSearchItem]) -> List[TrackSearchItem]:
    unique_tracks: Dict[Tuple[str, str, bool], TrackSearchItem] = {}

    for track in tracks:
        key = _track_key(track)
        current = unique_tracks.get(key)
        if current is None:
            unique_tracks[key] = track
            continue

        preferred, alternate = (track, current) if _track_rank(track) > _track_rank(current) else (current, track)
        unique_tracks[key] = _merge_tracks(preferred, alternate)

    return list(unique_tracks.values())


def _get_field(item: Any, name: str, default: Any = None) -> Any:
    if hasattr(item, name):
        value = getattr(item, name)
        return default if value is None else value
    if isinstance(item, dict):
        value = item.get(name)
        return default if value is None else value
    return default


class MusicBrainzClient:
    def __init__(self, timeout: int = 15, cache_file: str = ".musicbrainz_cache.db", cache_ttl_seconds: int = 604800):
        self.timeout = timeout
        self.base_url = "https://musicbrainz.org/ws/2"
        self.min_interval = 1.05
        self._last_request_at = 0.0
        self._cache: Dict[str, Dict[str, Any]] = {}
        self.cache_file = cache_file
        self.cache_ttl_seconds = cache_ttl_seconds
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": "MoraScraper/0.1.0 (jf.hh3002@gmail.com)",
                "Accept": "application/json",
            }
        )
        self._init_cache()

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

    def _init_cache(self) -> None:
        try:
            with sqlite3.connect(self.cache_file) as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS musicbrainz_cache (
                        cache_key TEXT PRIMARY KEY,
                        response TEXT NOT NULL,
                        timestamp REAL NOT NULL
                    )
                    """
                )
        except sqlite3.Error:
            pass

    def _cache_key_hash(self, cache_key: str) -> str:
        return hashlib.sha256(cache_key.encode("utf-8")).hexdigest()

    def _get_persistent_cache(self, cache_key: str) -> Dict[str, Any] | None:
        try:
            with sqlite3.connect(self.cache_file) as conn:
                row = conn.execute(
                    "SELECT response, timestamp FROM musicbrainz_cache WHERE cache_key = ?",
                    (self._cache_key_hash(cache_key),),
                ).fetchone()
            if not row:
                return None
            response, timestamp = row
            if time.time() - timestamp > self.cache_ttl_seconds:
                self._delete_persistent_cache(cache_key)
                return None
            return json.loads(response)
        except (sqlite3.Error, json.JSONDecodeError):
            return None

    def _set_persistent_cache(self, cache_key: str, data: Dict[str, Any]) -> None:
        try:
            with sqlite3.connect(self.cache_file) as conn:
                conn.execute(
                    "REPLACE INTO musicbrainz_cache (cache_key, response, timestamp) VALUES (?, ?, ?)",
                    (self._cache_key_hash(cache_key), json.dumps(data), time.time()),
                )
        except (sqlite3.Error, TypeError, ValueError):
            pass

    def _delete_persistent_cache(self, cache_key: str) -> None:
        try:
            with sqlite3.connect(self.cache_file) as conn:
                conn.execute(
                    "DELETE FROM musicbrainz_cache WHERE cache_key = ?",
                    (self._cache_key_hash(cache_key),),
                )
        except sqlite3.Error:
            pass

    def request(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        request_params = dict(params)
        request_params["fmt"] = "json"
        cache_key = f"{path}?{urllib.parse.urlencode(sorted((str(k), str(v)) for k, v in request_params.items()))}"
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        persistent_cached = self._get_persistent_cache(cache_key)
        if persistent_cached is not None:
            self._cache[cache_key] = persistent_cached
            return persistent_cached

        last_exc = None
        for attempt in range(4):
            self._throttle()
            try:
                resp = self.session.get(f"{self.base_url}{path}", params=request_params, timeout=self.timeout)
                self._last_request_at = time.monotonic()
                if resp.status_code == 503:
                    time.sleep(self.min_interval * (attempt + 1))
                    continue
                resp.raise_for_status()
                data = resp.json()
                self._cache[cache_key] = data
                self._set_persistent_cache(cache_key, data)
                return data
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(self.min_interval * (attempt + 1))
        raise last_exc or Exception(f"MusicBrainz request failed for {path}")

    def search_duration(self, title: str | None, artist: str | None, isrc: str | None) -> int:
        queries = []
        if isrc:
            queries.append(f'isrc:{isrc}')
        if title and artist:
            queries.append(f'recording:"{title}" AND artist:"{artist}"')
        elif title:
            queries.append(f'recording:"{title}"')

        target_title = normalize_text(title)
        target_artist = normalize_text(artist)
        for query in queries:
            try:
                data = self.request("/recording/", {"query": query, "limit": 10})
            except Exception:
                continue
            for item in data.get("recordings", []):
                duration = item.get("length")
                if not duration:
                    continue
                item_title = normalize_text(item.get("title"))
                artist_credit = item.get("artist-credit") or []
                item_artist = normalize_text((artist_credit[0].get("artist") or {}).get("name") if artist_credit else "")
                if target_title and item_title and target_title != item_title:
                    continue
                if target_artist and item_artist and target_artist not in item_artist:
                    continue
                return int(duration // 1000)
        return 0


_MUSICBRAINZ_CLIENT: MusicBrainzClient | None = None


def get_musicbrainz_client() -> MusicBrainzClient:
    global _MUSICBRAINZ_CLIENT
    if _MUSICBRAINZ_CLIENT is None:
        _MUSICBRAINZ_CLIENT = MusicBrainzClient()
    return _MUSICBRAINZ_CLIENT


class BaseMusicProvider:
    name = "base"
    supports_album_lookup = False
    supports_artist_lookup = False

    def __init__(self, timeout: int = 30, retries: int = 3):
        self.timeout = timeout
        self.retries = retries
        self.session = requests.Session()

    def _tag_artist(self, artist: Artist | Dict[str, Any] | None) -> Artist | None:
        if artist is None:
            return None
        if isinstance(artist, Artist):
            return artist.model_copy(update={"provider": self.name})
        return Artist(**artist, provider=self.name)

    def _tag_album(self, album: Album | Dict[str, Any] | None) -> Album | None:
        if album is None:
            return None
        if isinstance(album, Album):
            tagged_artist = self._tag_artist(album.artist) if album.artist else None
            return album.model_copy(update={"provider": self.name, "artist": tagged_artist})
        album_data = dict(album)
        album_artist = album_data.get("artist")
        if album_artist:
            album_data["artist"] = self._tag_artist(album_artist)
        return Album(**album_data, provider=self.name)

    def _tag_track(self, track: TrackSearchItem | TrackInfo) -> TrackSearchItem | TrackInfo:
        tagged_artist = self._tag_artist(track.artist) if track.artist else None
        tagged_artists = [self._tag_artist(artist) for artist in (track.artists or []) if artist]
        tagged_album = self._tag_album(track.album) if track.album else None
        return track.model_copy(
            update={
                "provider": self.name,
                "artist": tagged_artist,
                "artists": tagged_artists,
                "album": tagged_album,
            }
        )

    def search_tracks(self, query: str) -> List[TrackSearchItem]:
        raise NotImplementedError

    def get_track_info(self, track_id: int | str) -> TrackInfo:
        raise NotImplementedError

    def get_track_manifest(self, track_id: int | str, quality: str = "LOSSLESS") -> Dict[str, Any]:
        raise NotImplementedError

    def search_albums(self, query: str) -> List[AlbumCandidate]:
        normalized_query = normalize_text(query)
        groups: Dict[Tuple[str, str], List[TrackSearchItem]] = {}

        for track in self.search_tracks(query):
            if not track.album:
                continue
            album_key = normalize_text(track.album.title)
            if normalized_query and normalized_query not in album_key:
                continue
            artist_name = track.artist.name if track.artist else ""
            key = (album_key, normalize_text(artist_name))
            groups.setdefault(key, []).append(track)

        candidates: List[AlbumCandidate] = []
        for tracks in groups.values():
            best_track = tracks[0]
            candidates.append(
                AlbumCandidate(
                    id=best_track.album.id,
                    title=best_track.album.title,
                    artist=best_track.artist,
                    cover=best_track.album.cover,
                    provider=self.name,
                    tracks=deduplicate_tracks(tracks),
                )
            )

        return candidates

    def get_album_tracks(self, album: AlbumCandidate) -> List[TrackSearchItem]:
        artist_name = album.artist.name if album.artist else ""
        query = " ".join(part for part in [album.title, artist_name] if part).strip()
        matches = []
        target_album = normalize_text(album.title)
        for track in self.search_tracks(query or album.title):
            if track.album and normalize_text(track.album.title) == target_album:
                matches.append(track)
        return deduplicate_tracks(matches)

    def search_artists(self, query: str) -> List[ArtistCandidate]:
        normalized_query = normalize_text(query)
        groups: Dict[Tuple[str, str], List[TrackSearchItem]] = {}

        for track in self.search_tracks(query):
            artists = list(track.artists or [])
            if track.artist:
                artists.append(track.artist)
            for artist in artists:
                if not artist or not artist.name:
                    continue
                artist_key = normalize_text(artist.name)
                if normalized_query and normalized_query not in artist_key:
                    continue
                key = (artist_key, str(artist.id))
                groups.setdefault(key, []).append(track)

        candidates: List[ArtistCandidate] = []
        for tracks in groups.values():
            first_track = tracks[0]
            artist = first_track.artist or (first_track.artists[0] if first_track.artists else None)
            if not artist:
                continue
            candidates.append(
                ArtistCandidate(
                    id=artist.id,
                    name=artist.name,
                    picture=artist.picture,
                    provider=self.name,
                    tracks=deduplicate_tracks(tracks),
                )
            )
        return candidates

    def get_artist_tracks(self, artist: ArtistCandidate) -> List[TrackSearchItem]:
        target_name = normalize_text(artist.name)
        matches = []
        for track in self.search_tracks(artist.name):
            artists = list(track.artists or [])
            if track.artist:
                artists.append(track.artist)
            if any(a and normalize_text(a.name) == target_name for a in artists):
                matches.append(track)
        return deduplicate_tracks(matches)


class MusicBrainzMetadataProvider:
    name = "musicbrainz_metadata"

    def __init__(self, timeout: int = 15):
        self.timeout = timeout
        self.client = get_musicbrainz_client()

    def _request(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        return self.client.request(path, params)

    def _build_artist(self, artist_id: int | str | None, name: str | None) -> Artist:
        return Artist(id=artist_id or name or "unknown", name=name or "Unknown Artist", provider=self.name)

    def _quote_query(self, value: str) -> str:
        return '"' + value.replace('"', '\\"') + '"'

    def _artist_from_credit(self, credit: List[Dict[str, Any]] | None) -> tuple[Artist, List[Artist]]:
        names = []
        artists = []
        for part in credit or []:
            artist_data = part.get("artist") or {}
            artist = self._build_artist(artist_data.get("id"), artist_data.get("name") or part.get("name"))
            artist.type = artist_data.get("type")
            artist.area = (artist_data.get("area") or {}).get("name") or artist_data.get("country")
            artist.disambiguation = artist_data.get("disambiguation")
            names.append(part.get("name") or artist.name)
            artists.append(artist)
        if not artists:
            unknown = self._build_artist(None, "Unknown Artist")
            return unknown, [unknown]
        primary = artists[0].model_copy(update={"name": "".join(names) if names else artists[0].name})
        return primary, artists

    def _build_track_from_release(self, release_data: Dict[str, Any], medium_data: Dict[str, Any], track_data: Dict[str, Any]) -> TrackSearchItem:
        recording = track_data.get("recording") or {}
        artist, artists = self._artist_from_credit(
            track_data.get("artist-credit")
            or recording.get("artist-credit")
            or release_data.get("artist-credit")
        )
        album = Album(
            id=release_data.get("id", release_data.get("title", "")),
            title=release_data.get("title", ""),
            artist=artist.model_copy(update={"name": artist.name}),
            releaseDate=release_data.get("date"),
            disambiguation=release_data.get("disambiguation"),
            provider=self.name,
        )
        return TrackSearchItem(
            id=recording.get("id") or track_data.get("id") or f"{release_data.get('id')}:{track_data.get('id')}",
            title=track_data.get("title") or recording.get("title") or "",
            duration=int((track_data.get("length") or recording.get("length") or 0) / 1000),
            artist=artist,
            artists=artists,
            album=album,
            isrc=((recording.get("isrcs") or [None])[0]),
            releaseDate=release_data.get("date"),
            trackNumber=track_data.get("position"),
            volumeNumber=medium_data.get("position"),
            provider=self.name,
        )

    def search_albums(self, query: str, limit: int = 30) -> List[AlbumCandidate]:
        data = self._request(
            "/release/",
            {"query": f"release:{self._quote_query(query)}", "limit": limit},
        )
        candidates = []
        for item in data.get("releases", []):
            artist, _ = self._artist_from_credit(item.get("artist-credit"))
            candidates.append(
                AlbumCandidate(
                    id=item.get("id", item.get("title", "")),
                    title=item.get("title", ""),
                    artist=artist,
                    releaseDate=item.get("date"),
                    disambiguation=item.get("disambiguation"),
                    provider=self.name,
                )
            )
        return candidates

    def get_album_tracks(self, album: AlbumCandidate) -> List[TrackSearchItem]:
        album_data = self._request(
            f"/release/{album.id}",
            {"inc": "recordings+isrcs+artist-credits+release-groups+media"},
        )
        tracks = []
        for medium in album_data.get("media", []) or []:
            for track in medium.get("tracks", []) or []:
                tracks.append(self._build_track_from_release(album_data, medium, track))
        return deduplicate_tracks(tracks)

    def search_artists(self, query: str, limit: int = 12) -> List[ArtistCandidate]:
        data = self._request(
            "/artist/",
            {"query": f"artist:{self._quote_query(query)}", "limit": limit},
        )
        candidates = []
        for item in data.get("artists", []):
            artist = self._build_artist(item.get("id"), item.get("name"))
            candidates.append(
                ArtistCandidate(
                    id=artist.id,
                    name=artist.name,
                    type=item.get("type"),
                    area=(item.get("area") or {}).get("name") or item.get("country"),
                    disambiguation=item.get("disambiguation"),
                    provider=self.name,
                )
            )
        return candidates

    def get_artist_tracks(self, artist: ArtistCandidate, limit: int = 200) -> List[TrackSearchItem]:
        tracks = []
        offset = 0
        seen_release_ids = set()
        while len(tracks) < limit:
            data = self._request(
                "/release",
                {
                    "artist": artist.id,
                    "limit": 25,
                    "offset": offset,
                    "status": "official",
                    "type": "album|ep",
                    "inc": "recordings+isrcs+artist-credits+release-groups+media",
                },
            )
            releases = data.get("releases", [])
            if not releases:
                break
            for release in releases:
                release_id = release.get("id")
                if release_id in seen_release_ids:
                    continue
                seen_release_ids.add(release_id)
                for medium in release.get("media", []) or []:
                    for track in medium.get("tracks", []) or []:
                        tracks.append(self._build_track_from_release(release, medium, track))
                        if len(tracks) >= limit:
                            break
                    if len(tracks) >= limit:
                        break
                if len(tracks) >= limit:
                    break
            offset += len(releases)
            if offset >= data.get("release-count", 0):
                break

        return deduplicate_tracks(tracks[:limit])


class APIClient(BaseMusicProvider):
    name = "primary"
    supports_album_lookup = True
    supports_artist_lookup = True

    def __init__(self, timeout: int = 30, retries: int = 3):
        super().__init__(timeout=timeout, retries=retries)
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
                )
            }
        )
        self.hosts = [
            "https://hifi-two.spotisaver.net",
            "https://triton.squid.wtf",
            "https://hund.qqdl.site",
            "https://katze.qqdl.site",
            "https://api.monochrome.tf",
            "https://hifi-one.spotisaver.net",
            "https://singapore-1.monochrome.tf",
        ]
        self.search_timeout = min(4, timeout)
        self.search_retries = 1
        self.search_host_limit = 2
        self.host_cooldown_seconds = 45.0
        self._host_cooldowns: Dict[str, float] = {}

    def _get(self, url: str, params: Optional[Dict[str, Any]] = None, timeout: Optional[int] = None, retries: Optional[int] = None) -> Dict[str, Any]:
        request_timeout = timeout if timeout is not None else self.timeout
        request_retries = retries if retries is not None else self.retries
        for attempt in range(request_retries):
            try:
                resp = self.session.get(url, params=params, timeout=request_timeout)
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.RequestException:
                if attempt == request_retries - 1:
                    raise
                time.sleep(2 ** attempt)
        return {}

    def _candidate_hosts(self, max_hosts: Optional[int] = None) -> List[str]:
        now = time.monotonic()
        active_hosts = [host for host in self.hosts if self._host_cooldowns.get(host, 0.0) <= now]
        ordered_hosts = active_hosts or sorted(self.hosts, key=lambda host: self._host_cooldowns.get(host, 0.0))
        return ordered_hosts[:max_hosts] if max_hosts else ordered_hosts

    def _get_from_hosts(
        self,
        endpoint: str,
        params: Optional[Dict[str, Any]] = None,
        timeout: Optional[int] = None,
        retries: Optional[int] = None,
        max_hosts: Optional[int] = None,
    ) -> Dict[str, Any]:
        last_exc = None
        for base in self._candidate_hosts(max_hosts):
            try:
                return self._get(f"{base}{endpoint}", params, timeout=timeout, retries=retries)
            except requests.exceptions.RequestException as exc:
                self._host_cooldowns[base] = time.monotonic() + self.host_cooldown_seconds
                last_exc = exc
        if last_exc:
            raise last_exc
        raise Exception(f"Failed to fetch endpoint {endpoint}")

    def _set_highest_quality(self, item: Dict[str, Any]) -> None:
        bit_depth = item.get("bitDepth")
        sample_rate = item.get("sampleRate")
        if bit_depth is not None and sample_rate is not None:
            try:
                item["audioQuality"] = "HI_RES_LOSSLESS" if int(bit_depth) > 16 or int(sample_rate) > 44100 else "LOSSLESS"
                return
            except (TypeError, ValueError):
                pass

        tags = (item.get("mediaMetadata") or {}).get("tags") or []
        if "HIRES_LOSSLESS" in tags:
            item["audioQuality"] = "HI_RES_LOSSLESS"
        elif "LOSSLESS" in tags and item.get("audioQuality") != "HI_RES_LOSSLESS":
            item["audioQuality"] = "LOSSLESS"

    def search_tracks(self, query: str) -> List[TrackSearchItem]:
        data = self._get_from_hosts(
            "/search/",
            {"s": query},
            timeout=self.search_timeout,
            retries=self.search_retries,
            max_hosts=self.search_host_limit,
        )
        items = data.get("data", {}).get("items", [])
        results = []
        for item in items:
            self._set_highest_quality(item)
            results.append(self._tag_track(TrackSearchItem(**item)))
        return results

    def get_album(self, album_id: int) -> AlbumInfo:
        data = self._get_from_hosts("/album/", {"id": album_id})
        album_data = data.get("data", {})
        items = []
        for item in album_data.get("items", []):
            if "item" not in item:
                continue
            self._set_highest_quality(item["item"])
            items.append(item["item"])
        album_data["items"] = items
        return AlbumInfo(**album_data)

    def get_album_tracks(self, album: AlbumCandidate) -> List[TrackSearchItem]:
        try:
            album_info = self.get_album(int(album.id))
        except (TypeError, ValueError):
            return super().get_album_tracks(album)

        tracks = []
        for item in album_info.items:
            track = TrackSearchItem(**item)
            tracks.append(self._tag_track(track))
        return deduplicate_tracks(tracks)

    def get_track_info(self, track_id: int | str) -> TrackInfo:
        data = self._get_from_hosts("/info/", {"id": track_id})
        item_data = data.get("data", {})
        self._set_highest_quality(item_data)
        return self._tag_track(TrackInfo(**item_data))

    def get_artist(self, artist_id: int) -> ArtistInfo:
        data = self._get_from_hosts("/artist/", {"f": artist_id})
        albums = data.get("albums", {}).get("items", [])
        tracks_data = data.get("tracks", [])

        normalized_tracks = []
        for track in tracks_data:
            if "artist" not in track and track.get("artists"):
                track["artist"] = track["artists"][0]
            self._set_highest_quality(track)
            normalized_tracks.append(track)

        tracks = [self._tag_track(TrackSearchItem(**track)) for track in normalized_tracks]
        return ArtistInfo(albums=albums, tracks=tracks)

    def get_artist_tracks(self, artist: ArtistCandidate) -> List[TrackSearchItem]:
        try:
            artist_info = self.get_artist(int(artist.id))
        except (TypeError, ValueError):
            return super().get_artist_tracks(artist)
        return deduplicate_tracks(artist_info.tracks)

    def get_track_manifest(self, track_id: int | str, quality: str = "LOSSLESS") -> Dict[str, Any]:
        for base in self.hosts:
            try:
                data = self._get(f"{base}/track/", {"id": track_id, "quality": quality})
                inner = data.get("data", {})
                if inner.get("manifest"):
                    self._set_highest_quality(inner)
                    return data
            except requests.exceptions.RequestException:
                continue
        raise Exception(f"Could not fetch a valid manifest for track {track_id} with quality {quality}")


class AmazonAPIClient(BaseMusicProvider):
    name = "amazon"
    supports_album_lookup = False
    supports_artist_lookup = False

    def __init__(self, timeout: int = 30, retries: int = 3):
        super().__init__(timeout=timeout, retries=retries)
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Content-Type": "application/json",
            }
        )
        self.base_url = "https://amz.redsleaks.to/api"
        self._track_meta_cache: Dict[str, Dict[str, Any]] = {}
        self._duration_cache: Dict[str, int] = {}
        self._musicbrainz_client = get_musicbrainz_client()
        self.search_timeout = min(6, timeout)
        self.search_retries = 1

    def _post(
        self,
        endpoint: str,
        json_data: Dict[str, Any],
        timeout: Optional[int] = None,
        retries: Optional[int] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}/{endpoint}"
        request_timeout = timeout if timeout is not None else self.timeout
        request_retries = retries if retries is not None else self.retries
        for attempt in range(request_retries):
            try:
                resp = self.session.post(url, json=json_data, timeout=request_timeout)
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.RequestException:
                if attempt == request_retries - 1:
                    raise
                time.sleep(2 ** attempt)
        return {}

    def _duration_from_synced_lyrics(self, synced_lyrics: str | None) -> int:
        if not synced_lyrics:
            return 0
        matches = re.findall(r"\[(\d{2}):(\d{2})(?:\.(\d{2}))?\]", synced_lyrics)
        if not matches:
            return 0
        mins, secs, centis = matches[-1]
        return int(mins) * 60 + int(secs) + (1 if centis and int(centis) >= 50 else 0)

    def _duration_from_musicbrainz(self, title: str | None, artist: str | None, isrc: str | None) -> int:
        cache_key = f"{isrc or ''}|{title or ''}|{artist or ''}"
        if cache_key in self._duration_cache:
            return self._duration_cache[cache_key]
        self._duration_cache[cache_key] = self._musicbrainz_client.search_duration(title, artist, isrc)
        return self._duration_cache[cache_key]

    def _get_track_metadata(self, asin: str) -> Dict[str, Any]:
        if asin not in self._track_meta_cache:
            data = self._post("track", {"asin": asin, "tier": "best", "country": "US"})
            self._track_meta_cache[asin] = data.get("metadata", {})
        return self._track_meta_cache[asin]

    def _extract_duration(self, item: Dict[str, Any], metadata: Dict[str, Any] | None = None, allow_fallback: bool = True) -> int:
        metadata = metadata or {}
        duration = int(
            item.get("duration")
            or metadata.get("duration")
            or item.get("durationMs", 0) // 1000
            or metadata.get("durationMs", 0) // 1000
            or item.get("duration_ms", 0) // 1000
            or metadata.get("duration_ms", 0) // 1000
            or 0
        )
        if duration or not allow_fallback:
            return duration
        return (
            self._duration_from_synced_lyrics((metadata.get("lyrics") or {}).get("synced"))
            or self._duration_from_musicbrainz(
                metadata.get("title") or item.get("title"),
                metadata.get("artist") or item.get("primaryArtistName") or item.get("artistName"),
                metadata.get("isrc"),
            )
            or 0
        )

    def _build_track_search_item(
        self,
        item: Dict[str, Any],
        metadata: Dict[str, Any] | None = None,
        allow_duration_fallback: bool = True,
    ) -> TrackSearchItem:
        metadata = metadata or {}
        artist_name = metadata.get("artist") or item.get("primaryArtistName") or item.get("artistName") or "Unknown Artist"
        album_title = (
            metadata.get("album")
            or (item.get("album") or {}).get("title")
            or item.get("albumName")
            or ""
        )
        explicit = bool(metadata.get("is_explicit") or "[Explicit]" in item.get("title", ""))
        duration = self._extract_duration(item, metadata, allow_fallback=allow_duration_fallback)

        artist = Artist(id=artist_name, name=artist_name, provider=self.name)
        album = Album(
            id=metadata.get("album", "") or album_title or "",
            title=album_title,
            cover=metadata.get("cover") or (item.get("album") or {}).get("image"),
            artist=artist,
            provider=self.name,
        )

        track_number = None
        if metadata.get("track_number"):
            try:
                track_number = int(metadata["track_number"])
            except (ValueError, TypeError):
                pass
        disc_number = None
        if metadata.get("disc_number"):
            try:
                disc_number = int(metadata["disc_number"])
            except (ValueError, TypeError):
                pass
        track_total = None
        if metadata.get("track_total"):
            try:
                track_total = int(metadata["track_total"])
            except (ValueError, TypeError):
                pass
        disc_total = None
        if metadata.get("disc_total"):
            try:
                disc_total = int(metadata["disc_total"])
            except (ValueError, TypeError):
                pass

        return TrackSearchItem(
            id=item.get("asin") or metadata.get("asin", ""),
            title=metadata.get("title") or item.get("title", "") or "Unknown Title",
            duration=duration,
            artist=artist,
            artists=[artist],
            album=album,
            explicit=explicit,
            audioQuality="LOSSLESS",
            isrc=metadata.get("isrc"),
            copyright=metadata.get("copyright"),
            genre=metadata.get("genre"),
            label=metadata.get("label"),
            composer=metadata.get("composer"),
            releaseDate=metadata.get("date"),
            trackNumber=track_number,
            volumeNumber=disc_number,
            trackTotal=track_total,
            discTotal=disc_total,
            provider=self.name,
        )

    def search_tracks(self, query: str) -> List[TrackSearchItem]:
        data = self._post(
            "search",
            {"query": query, "country": "US", "content_type": "TRACK", "limit": 16},
            timeout=self.search_timeout,
            retries=self.search_retries,
        )
        results = []
        for item in data.get("trackList", []):
            asin = item.get("asin")
            if not asin:
                continue
            try:
                track = self._build_track_search_item(item, allow_duration_fallback=False)
                results.append(self._tag_track(track))
            except Exception:
                continue
        return results

    def get_track_info(self, track_id: int | str) -> TrackInfo:
        asin = str(track_id)
        meta = self._get_track_metadata(asin)
        artist_name = meta.get("artist", "Unknown")
        artist = Artist(id=artist_name, name=artist_name, provider=self.name)
        album = Album(
            id=meta.get("album", "") or "",
            title=meta.get("album", ""),
            cover=meta.get("cover"),
            artist=artist,
            provider=self.name,
        )

        track_number = None
        if meta.get("track_number"):
            try:
                track_number = int(meta["track_number"])
            except (ValueError, TypeError):
                pass
        disc_number = None
        if meta.get("disc_number"):
            try:
                disc_number = int(meta["disc_number"])
            except (ValueError, TypeError):
                pass
        track_total = None
        if meta.get("track_total"):
            try:
                track_total = int(meta["track_total"])
            except (ValueError, TypeError):
                pass
        disc_total = None
        if meta.get("disc_total"):
            try:
                disc_total = int(meta["disc_total"])
            except (ValueError, TypeError):
                pass

        track = TrackInfo(
            id=meta.get("asin", asin),
            title=meta.get("title", ""),
            duration=(
                self._duration_from_synced_lyrics((meta.get("lyrics") or {}).get("synced"))
                or self._duration_from_musicbrainz(meta.get("title"), artist_name, meta.get("isrc"))
            ),
            artist=artist,
            artists=[artist],
            album=album,
            explicit=meta.get("is_explicit", False),
            audioQuality="LOSSLESS",
            isrc=meta.get("isrc"),
            copyright=meta.get("copyright"),
            genre=meta.get("genre"),
            label=meta.get("label"),
            composer=meta.get("composer"),
            releaseDate=meta.get("date"),
            trackNumber=track_number,
            volumeNumber=disc_number,
            trackTotal=track_total,
            discTotal=disc_total,
            provider=self.name,
        )
        return self._tag_track(track)

    def get_track_manifest(self, track_id: int | str, quality: str = "LOSSLESS") -> Dict[str, Any]:
        asin = str(track_id)
        data = self._post("track", {"asin": asin, "tier": "best", "country": "US"})
        stream = data.get("stream", {})
        stream_url = stream.get("url")
        if not stream_url:
            raise Exception(f"Could not fetch a valid stream for track {asin}")

        codec = stream.get("codec", "flac")
        return {
            "data": {
                "manifestMimeType": "direct",
                "url": f"https://amz.redsleaks.to{stream_url}",
                "codec": codec,
                "streamCodec": codec,
                "streamTier": "best",
                "resolvedQuality": "LOSSLESS" if codec == "flac" else "HIGH",
                "drm_key": data.get("drm", {}).get("key"),
            }
        }


class CatalogService:
    def __init__(self, providers: List[BaseMusicProvider], metadata_provider: MusicBrainzMetadataProvider | None = None):
        self.providers = providers
        self.provider_map = {provider.name: provider for provider in providers}
        self.metadata_provider = metadata_provider
        self.search_timeout_seconds = 8.0
        self.search_grace_seconds = 0.35
        self.provider_cooldown_seconds = 60.0
        self._provider_search_cooldowns: Dict[str, float] = {}

    def _ordered_providers(self, preferred: str | None = None) -> List[BaseMusicProvider]:
        return sorted(self.providers, key=lambda provider: provider.name != preferred)

    def _collection_score(self, tracks: List[TrackSearchItem], provider_name: str | None = None) -> Tuple[int, int, int]:
        provider = self.provider_map.get(provider_name) if provider_name else None
        supports_lookup = 1 if provider and (provider.supports_album_lookup or provider.supports_artist_lookup) else 0
        total_duration = sum(track.duration or 0 for track in tracks)
        return (len(tracks), supports_lookup, total_duration)

    def _search_provider(self, provider: BaseMusicProvider, query: str, title_only_fallback: str | None = None) -> List[TrackSearchItem]:
        for search_term in [query, title_only_fallback]:
            if not search_term:
                continue
            try:
                results = provider.search_tracks(search_term)
            except Exception:
                self._provider_search_cooldowns[provider.name] = time.monotonic() + self.provider_cooldown_seconds
                return []
            if results:
                return results
        return []

    def _sort_by_relevance(self, items: List[TrackSearchItem], query: str) -> List[TrackSearchItem]:
        """Sort deduplicated tracks by relevance to the original query."""
        return sorted(
            items,
            key=lambda t: (
                -_relevance_score(query, t.title, _track_primary_artist_name(t)),
                -track_metadata_score(t),
                -quality_rank(t),
                -(t.duration or 0),
            ),
        )

    def search_tracks(self, query: str, title_only_fallback: str | None = None) -> List[TrackSearchItem]:
        now = time.monotonic()
        active_providers = [
            provider for provider in self.providers
            if self._provider_search_cooldowns.get(provider.name, 0.0) <= now
        ] or self.providers

        executor = ThreadPoolExecutor(max_workers=len(active_providers))
        future_to_provider = {
            executor.submit(self._search_provider, provider, query, title_only_fallback): provider
            for provider in active_providers
        }
        pending = set(future_to_provider)
        collected: Dict[str, List[TrackSearchItem]] = {}
        deadline = time.monotonic() + self.search_timeout_seconds
        first_success_at: float | None = None

        try:
            while pending and time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                done, pending = wait(pending, timeout=min(0.25, remaining), return_when=FIRST_COMPLETED)
                for future in done:
                    provider = future_to_provider[future]
                    try:
                        results = future.result()
                    except Exception:
                        self._provider_search_cooldowns[provider.name] = time.monotonic() + self.provider_cooldown_seconds
                        continue
                    if results:
                        collected[provider.name] = results
                        if first_success_at is None:
                            first_success_at = time.monotonic()
                if first_success_at is not None and time.monotonic() - first_success_at >= self.search_grace_seconds:
                    break
        finally:
            for future in pending:
                future.cancel()
            executor.shutdown(wait=False, cancel_futures=True)

        ordered_results: List[TrackSearchItem] = []
        for provider in self.providers:
            ordered_results.extend(collected.get(provider.name, []))
        return self._sort_by_relevance(deduplicate_tracks(ordered_results), query)

    def search_albums(self, query: str) -> List[AlbumCandidate]:
        if self.metadata_provider is not None:
            try:
                metadata_candidates = self.metadata_provider.search_albums(query)
                if metadata_candidates:
                    return sorted(
                        metadata_candidates,
                        key=lambda a: -_relevance_score(
                            query, a.title, a.artist.name if a.artist else ""
                        ),
                    )
            except Exception:
                logger.debug("Metadata provider search_albums failed", exc_info=True)

        groups: Dict[Tuple[str, str], AlbumCandidate] = {}
        for provider in self.providers:
            try:
                candidates = provider.search_albums(query)
            except Exception:
                logger.debug("Provider %s search_albums failed", provider.name, exc_info=True)
                continue

            for candidate in candidates:
                artist_name = candidate.artist.name if candidate.artist else ""
                key = (normalize_text(candidate.title), normalize_text(artist_name))
                current = groups.get(key)
                if current is None:
                    groups[key] = candidate
                    continue

                merged_tracks = deduplicate_tracks(current.tracks + candidate.tracks)
                preferred = candidate if self._is_better_album_candidate(candidate, current) else current
                groups[key] = preferred.model_copy(update={"tracks": merged_tracks})

        return sorted(
            groups.values(),
            key=lambda album: (
                -_relevance_score(query, album.title, album.artist.name if album.artist else ""),
                not self.provider_map[album.provider].supports_album_lookup,
                -len(album.tracks),
            ),
        )

    def _is_better_album_candidate(self, new: AlbumCandidate, current: AlbumCandidate) -> bool:
        new_provider = self.provider_map[new.provider]
        current_provider = self.provider_map[current.provider]
        if new_provider.supports_album_lookup != current_provider.supports_album_lookup:
            return new_provider.supports_album_lookup
        return len(new.tracks) > len(current.tracks)

    def get_album_tracks(self, album: AlbumCandidate) -> List[TrackSearchItem]:
        if self.metadata_provider is not None and album.provider == self.metadata_provider.name:
            try:
                metadata_tracks = self.metadata_provider.get_album_tracks(album)
                if metadata_tracks:
                    return metadata_tracks
            except Exception:
                pass

        best_tracks = deduplicate_tracks(album.tracks)
        best_provider = album.provider

        for provider in self._ordered_providers(album.provider):
            try:
                if provider.name == album.provider:
                    tracks = provider.get_album_tracks(album)
                else:
                    fallback_album = album.model_copy(update={"id": "", "provider": provider.name})
                    tracks = provider.get_album_tracks(fallback_album)
                tracks = deduplicate_tracks(tracks)
                if not tracks:
                    continue
                if self._collection_score(tracks, provider.name) > self._collection_score(best_tracks, best_provider):
                    best_tracks = tracks
                    best_provider = provider.name
            except Exception:
                continue
        return best_tracks

    def search_artists(self, query: str) -> List[ArtistCandidate]:
        if self.metadata_provider is not None:
            try:
                metadata_candidates = self.metadata_provider.search_artists(query)
                if metadata_candidates:
                    return sorted(
                        metadata_candidates,
                        key=lambda a: -_relevance_score(query, a.name),
                    )
            except Exception:
                logger.debug("Metadata provider search_artists failed", exc_info=True)

        groups: Dict[Tuple[str, str], ArtistCandidate] = {}
        for provider in self.providers:
            try:
                candidates = provider.search_artists(query)
            except Exception:
                logger.debug("Provider %s search_artists failed", provider.name, exc_info=True)
                continue

            for candidate in candidates:
                key = (normalize_text(candidate.name), str(candidate.id))
                current = groups.get(key)
                if current is None:
                    groups[key] = candidate
                    continue

                merged_tracks = deduplicate_tracks(current.tracks + candidate.tracks)
                preferred = candidate if self._is_better_artist_candidate(candidate, current) else current
                groups[key] = preferred.model_copy(update={"tracks": merged_tracks})

        return sorted(
            groups.values(),
            key=lambda artist: (
                -_relevance_score(query, artist.name),
                not self.provider_map[artist.provider].supports_artist_lookup,
                -len(artist.tracks),
            ),
        )

    def _is_better_artist_candidate(self, new: ArtistCandidate, current: ArtistCandidate) -> bool:
        new_provider = self.provider_map[new.provider]
        current_provider = self.provider_map[current.provider]
        if new_provider.supports_artist_lookup != current_provider.supports_artist_lookup:
            return new_provider.supports_artist_lookup
        return len(new.tracks) > len(current.tracks)

    def get_artist_tracks(self, artist: ArtistCandidate) -> List[TrackSearchItem]:
        if self.metadata_provider is not None and artist.provider == self.metadata_provider.name:
            try:
                metadata_tracks = self.metadata_provider.get_artist_tracks(artist)
                if metadata_tracks:
                    return metadata_tracks
            except Exception:
                pass

        best_tracks = deduplicate_tracks(artist.tracks)
        best_provider = artist.provider

        for provider in self._ordered_providers(artist.provider):
            try:
                if provider.name == artist.provider:
                    tracks = provider.get_artist_tracks(artist)
                else:
                    fallback_artist = artist.model_copy(update={"id": artist.name, "provider": provider.name})
                    tracks = provider.get_artist_tracks(fallback_artist)
                tracks = deduplicate_tracks(tracks)
                if not tracks:
                    continue
                if self._collection_score(tracks, provider.name) > self._collection_score(best_tracks, best_provider):
                    best_tracks = tracks
                    best_provider = provider.name
            except Exception:
                continue
        return best_tracks

    def resolve_track_match(self, track_ref: TrackSearchItem, provider: BaseMusicProvider) -> TrackSearchItem | None:
        query = " ".join(part for part in [track_ref.title, track_ref.artist.name if track_ref.artist else ""] if part).strip()
        for search_term in [query, track_ref.title]:
            if not search_term:
                continue
            try:
                results = provider.search_tracks(search_term)
            except Exception:
                break
            match = self._find_best_track_match(track_ref, results)
            if match:
                return match
        return None

    def _find_best_track_match(self, target: TrackSearchItem, results: List[TrackSearchItem]) -> TrackSearchItem | None:
        target_title = normalize_text(target.title)
        target_album = normalize_text(target.album.title if target.album else "")
        target_artists = {normalize_text(artist.name) for artist in (target.artists or []) if artist and artist.name}
        if target.artist and target.artist.name:
            target_artists.add(normalize_text(target.artist.name))

        exact_matches = []
        partial_matches = []
        for candidate in deduplicate_tracks(results):
            candidate_title = normalize_text(candidate.title)
            candidate_album = normalize_text(candidate.album.title if candidate.album else "")
            candidate_artists = {normalize_text(artist.name) for artist in (candidate.artists or []) if artist and artist.name}
            if candidate.artist and candidate.artist.name:
                candidate_artists.add(normalize_text(candidate.artist.name))

            title_matches = not target_title or candidate_title == target_title
            artist_matches = not target_artists or bool(target_artists & candidate_artists)
            album_matches = not target_album or candidate_album == target_album

            if title_matches and artist_matches and album_matches:
                exact_matches.append(candidate)
            elif title_matches and artist_matches:
                partial_matches.append(candidate)

        if exact_matches:
            return exact_matches[0]
        if partial_matches:
            return partial_matches[0]
        deduped = deduplicate_tracks(results)
        return deduped[0] if deduped else None

    def resolve_download(self, track_ref: TrackSearchItem, quality: str) -> Tuple[TrackInfo, Dict[str, Any], str]:
        preferred_provider = track_ref.provider
        last_exc = None

        for provider in self._ordered_providers(preferred_provider):
            try:
                candidate_id = track_ref.id if provider.name == preferred_provider else None
                if candidate_id is None:
                    matched = self.resolve_track_match(track_ref, provider)
                    if matched is None:
                        continue
                    candidate_id = matched.id

                info = provider.get_track_info(candidate_id)
                try:
                    manifest = provider.get_track_manifest(candidate_id, quality)
                except Exception:
                    alt_quality = "HI_RES_LOSSLESS" if quality == "LOSSLESS" else "LOSSLESS"
                    manifest = provider.get_track_manifest(candidate_id, alt_quality)

                manifest_data = manifest.get("data", {})
                manifest_quality = (
                    manifest_data.get("resolvedQuality")
                    or manifest_data.get("audioQuality")
                    or info.audioQuality
                )
                enriched_info = info.model_copy(
                    update={
                        "bitDepth": manifest_data.get("bitDepth", info.bitDepth),
                        "sampleRate": manifest_data.get("sampleRate", info.sampleRate),
                        "streamCodec": manifest_data.get("streamCodec", info.streamCodec),
                        "streamTier": manifest_data.get("streamTier", info.streamTier),
                        "resolvedQuality": manifest_data.get("resolvedQuality", manifest_quality),
                        "audioQuality": manifest_data.get("audioQuality", info.audioQuality or manifest_quality),
                    }
                )
                return enriched_info, manifest, provider.name
            except Exception as exc:
                last_exc = exc

        raise last_exc or Exception("no available provider")

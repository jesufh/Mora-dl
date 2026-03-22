import time
import requests
import re
import urllib.parse
from typing import Optional, List, Dict, Any
from .models import TrackSearchItem, AlbumInfo, ArtistInfo, TrackInfo, Album, Artist

class APIClient:
    def __init__(self, timeout: int = 30, retries: int = 3):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
        })
        self.timeout = timeout
        self.retries = retries
        self.hosts = [
            "https://hifi-two.spotisaver.net",
            "https://triton.squid.wtf",
            "https://hund.qqdl.site",
            "https://katze.qqdl.site",
            "https://api.monochrome.tf",
            "https://hifi-one.spotisaver.net",
            "https://singapore-1.monochrome.tf",
        ]

    def _get(self, url: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        for attempt in range(self.retries):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.RequestException:
                if attempt == self.retries - 1:
                    raise
                time.sleep(2 ** attempt)

    def _get_from_hosts(self, endpoint: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        last_exc = None
        for base in self.hosts:
            url = f"{base}{endpoint}"
            try:
                return self._get(url, params)
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                continue
        if last_exc:
            raise last_exc
        raise Exception(f"Failed to fetch endpoint {endpoint}")

    def _set_highest_quality(self, item: Dict[str, Any]) -> None:
        media_metadata = item.get("mediaMetadata") or {}
        tags = media_metadata.get("tags") or[]
        if "HIRES_LOSSLESS" in tags:
            item["audioQuality"] = "HI_RES_LOSSLESS"
        elif "LOSSLESS" in tags and item.get("audioQuality") != "HI_RES_LOSSLESS":
            item["audioQuality"] = "LOSSLESS"

    def search_tracks(self, query: str) -> List[TrackSearchItem]:
        data = self._get_from_hosts("/search/", {"s": query})
        items = data.get("data", {}).get("items",[])
        for i in items:
            self._set_highest_quality(i)
        return [TrackSearchItem(**i) for i in items]

    def get_album(self, album_id: int) -> AlbumInfo:
        data = self._get_from_hosts("/album/", {"id": album_id})
        album_data = data.get("data", {})
        items =[]
        for it in album_data.get("items",[]):
            if "item" in it:
                self._set_highest_quality(it["item"])
                items.append(it["item"])
        album_data["items"] = items
        return AlbumInfo(**album_data)

    def get_track_info(self, track_id: int) -> TrackInfo:
        data = self._get_from_hosts("/info/", {"id": track_id})
        item_data = data.get("data", {})
        self._set_highest_quality(item_data)
        return TrackInfo(**item_data)

    def get_artist(self, artist_id: int) -> ArtistInfo:
        data = self._get_from_hosts("/artist/", {"f": artist_id})
        albums = data.get("albums", {}).get("items",[])
        tracks_data = data.get("tracks", [])
        
        normalized_tracks =[]
        for t in tracks_data:
            if 'artist' not in t and 'artists' in t and t['artists']:
                t['artist'] = t['artists'][0]
            self._set_highest_quality(t)
            normalized_tracks.append(t)
            
        tracks =[TrackSearchItem(**t) for t in normalized_tracks]
        return ArtistInfo(albums=albums, tracks=tracks)

    def get_track_manifest(self, track_id: int, quality: str = "LOSSLESS") -> Dict[str, Any]:
        for base in self.hosts:
            url = f"{base}/track/"
            try:
                data = self._get(url, {"id": track_id, "quality": quality})
                inner = data.get("data", {})
                if inner.get("manifest"):
                    return data
            except requests.exceptions.RequestException:
                continue
        raise Exception(f"Could not fetch a valid manifest for track {track_id} with quality {quality}")

class AmazonAPIClient:
    def __init__(self, timeout: int = 30, retries: int = 3):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Content-Type": "application/json"
        })
        self.base_url = "https://amz.redsleaks.to/api"
        self.timeout = timeout
        self.retries = retries
        self._track_meta_cache: Dict[str, Dict[str, Any]] = {}
        self._duration_cache: Dict[str, int] = {}

    def _duration_from_synced_lyrics(self, synced_lyrics: str | None) -> int:
        if not synced_lyrics:
            return 0
        matches = re.findall(r"\[(\d{2}):(\d{2})(?:\.(\d{2}))?\]", synced_lyrics)
        if not matches:
            return 0
        mins, secs, centis = matches[-1]
        return int(mins) * 60 + int(secs) + (1 if centis and int(centis) >= 50 else 0)

    def _normalize_text(self, value: str | None) -> str:
        if not value:
            return ""
        return re.sub(r"[^a-z0-9]", "", value.lower())

    def _duration_from_itunes(self, title: str | None, artist: str | None, isrc: str | None) -> int:
        cache_key = f"{isrc or ''}|{title or ''}|{artist or ''}"
        if cache_key in self._duration_cache:
            return self._duration_cache[cache_key]

        queries = []
        if isrc:
            queries.append(isrc)
        if title and artist:
            queries.append(f"{title} {artist}")
        elif title:
            queries.append(title)

        target_title = self._normalize_text(title)
        target_artist = self._normalize_text(artist)

        for query in queries:
            try:
                encoded_q = urllib.parse.quote(query)
                url = f"https://itunes.apple.com/search?term={encoded_q}&entity=song&limit=10"
                resp = self.session.get(url, timeout=self.timeout)
                resp.raise_for_status()
                results = resp.json().get("results", [])

                for item in results:
                    millis = item.get("trackTimeMillis")
                    if not millis:
                        continue
                    item_title = self._normalize_text(item.get("trackName", ""))
                    item_artist = self._normalize_text(item.get("artistName", ""))

                    if isrc and query == isrc:
                        if target_title and item_title and target_title != item_title:
                            continue
                    if target_title and item_title and target_title != item_title:
                        continue
                    if target_artist and item_artist and target_artist not in item_artist:
                        continue

                    self._duration_cache[cache_key] = int(millis // 1000)
                    return self._duration_cache[cache_key]
            except requests.RequestException:
                continue

        self._duration_cache[cache_key] = 0
        return 0

    def _get_track_metadata(self, asin: str) -> Dict[str, Any]:
        if asin not in self._track_meta_cache:
            data = self._post("track", {"asin": asin, "tier": "best", "country": "US"})
            self._track_meta_cache[asin] = data.get("metadata", {})
        return self._track_meta_cache[asin]

    def _build_track_search_item(self, item: Dict[str, Any], metadata: Dict[str, Any] | None = None) -> TrackSearchItem:
        metadata = metadata or {}
        album_data = item.get("album", {})
        artist_name = (
            metadata.get("artist")
            or item.get("primaryArtistName")
            or item.get("artistName")
            or "Unknown Artist"
        )
        album_title = (
            metadata.get("album")
            or album_data.get("title")
            or ""
        )
        explicit = bool(metadata.get("is_explicit") or "[Explicit]" in item.get("title", ""))
        duration = int(
            item.get("duration")
            or item.get("durationMs", 0) // 1000
            or item.get("duration_ms", 0) // 1000
            or self._duration_from_synced_lyrics((metadata.get("lyrics") or {}).get("synced"))
            or self._duration_from_itunes(
                metadata.get("title") or item.get("title"),
                artist_name,
                metadata.get("isrc"),
            )
            or 0
        )

        album = Album(
            id=metadata.get("album", "") or album_title or "",
            title=album_title,
            cover=metadata.get("cover") or album_data.get("image")
        )
        artist = Artist(id=artist_name, name=artist_name)

        return TrackSearchItem(
            id=item.get("asin") or metadata.get("asin", ""),
            title=metadata.get("title") or item.get("title", ""),
            duration=duration,
            artist=artist,
            artists=[artist],
            album=album,
            explicit=explicit,
            audioQuality="LOSSLESS",
            isrc=metadata.get("isrc"),
            copyright=metadata.get("copyright"),
            releaseDate=metadata.get("date"),
            trackNumber=int(metadata.get("track_number")) if metadata.get("track_number") else None,
            volumeNumber=int(metadata.get("disc_number")) if metadata.get("disc_number") else None,
        )

    def _post(self, endpoint: str, json_data: dict) -> dict:
        url = f"{self.base_url}/{endpoint}"
        for attempt in range(self.retries):
            try:
                resp = self.session.post(url, json=json_data, timeout=self.timeout)
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.RequestException:
                if attempt == self.retries - 1:
                    raise
                time.sleep(2 ** attempt)

    def search_tracks(self, query: str) -> List[TrackSearchItem]:
        data = self._post("search", {"query": query, "country": "US", "content_type": "TRACK", "limit": 24})
        items = []
        for item in data.get("trackList", []):
            try:
                asin = item.get("asin")
                metadata = self._get_track_metadata(asin) if asin else {}
                items.append(self._build_track_search_item(item, metadata))
            except Exception:
                continue
        return items

    def get_track_info(self, asin: str) -> TrackInfo:
        meta = self._get_track_metadata(asin)
        artist_name = meta.get("artist", "Unknown")
        album = Album(id=meta.get("album", "") or "", title=meta.get("album", ""), cover=meta.get("cover"))
        artist = Artist(id=artist_name, name=artist_name)

        return TrackInfo(
            id=meta.get("asin", asin),
            title=meta.get("title", ""),
            duration=(
                self._duration_from_synced_lyrics((meta.get("lyrics") or {}).get("synced"))
                or self._duration_from_itunes(meta.get("title"), artist_name, meta.get("isrc"))
            ),
            artist=artist,
            artists=[artist],
            album=album,
            explicit=meta.get("is_explicit", False),
            audioQuality="LOSSLESS",
            isrc=meta.get("isrc"),
            copyright=meta.get("copyright"),
            releaseDate=meta.get("date"),
            trackNumber=int(meta.get("track_number", 1)) if meta.get("track_number") else None,
            volumeNumber=int(meta.get("disc_number", 1)) if meta.get("disc_number") else None
        )

    def get_track_manifest(self, asin: str, quality: str = "LOSSLESS") -> Dict[str, Any]:
        data = self._post("track", {"asin": asin, "tier": "best", "country": "US"})
        stream = data.get("stream", {})
        stream_url = stream.get("url")
        if not stream_url:
            raise Exception(f"Could not fetch a valid stream for track {asin}")

        full_url = f"https://amz.redsleaks.to{stream_url}"
        return {
            "data": {
                "manifestMimeType": "direct",
                "url": full_url,
                "codec": stream.get("codec", "flac"),
                "drm_key": data.get("drm", {}).get("key")
            }
        }

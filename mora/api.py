import time
import requests
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
        self.search_base = "https://hifi-two.spotisaver.net"
        self.album_base = "https://hifi-two.spotisaver.net"
        self.info_base = "https://triton.squid.wtf"
        self.artist_base = "https://triton.squid.wtf"
        self.track_bases =["https://hund.qqdl.site", "https://katze.qqdl.site"]

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

    def _set_highest_quality(self, item: Dict[str, Any]) -> None:
        media_metadata = item.get("mediaMetadata") or {}
        tags = media_metadata.get("tags") or[]
        if "HIRES_LOSSLESS" in tags:
            item["audioQuality"] = "HI_RES_LOSSLESS"
        elif "LOSSLESS" in tags and item.get("audioQuality") != "HI_RES_LOSSLESS":
            item["audioQuality"] = "LOSSLESS"

    def search_tracks(self, query: str) -> List[TrackSearchItem]:
        url = f"{self.search_base}/search/"
        data = self._get(url, {"s": query})
        items = data.get("data", {}).get("items",[])
        for i in items:
            self._set_highest_quality(i)
        return [TrackSearchItem(**i) for i in items]

    def get_album(self, album_id: int) -> AlbumInfo:
        url = f"{self.album_base}/album/"
        data = self._get(url, {"id": album_id})
        album_data = data.get("data", {})
        items =[]
        for it in album_data.get("items",[]):
            if "item" in it:
                self._set_highest_quality(it["item"])
                items.append(it["item"])
        album_data["items"] = items
        return AlbumInfo(**album_data)

    def get_track_info(self, track_id: int) -> TrackInfo:
        url = f"{self.info_base}/info/"
        data = self._get(url, {"id": track_id})
        item_data = data.get("data", {})
        self._set_highest_quality(item_data)
        return TrackInfo(**item_data)

    def get_artist(self, artist_id: int) -> ArtistInfo:
        url = f"{self.artist_base}/artist/"
        data = self._get(url, {"f": artist_id})
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
        for base in self.track_bases:
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
        items =[]
        for item in data.get("trackList",[]):
            try:
                album_data = item.get("album", {})
                artist_data = item.get("artist", {})
                
                album = Album(
                    id=album_data.get("asin", ""),
                    title=album_data.get("title", "Unknown Album"),
                    cover=album_data.get("image")
                )
                
                artist = Artist(
                    id=artist_data.get("asin", ""),
                    name=artist_data.get("name", item.get("primaryArtistName", "Unknown Artist"))
                )
                
                items.append(TrackSearchItem(
                    id=item.get("asin"),
                    title=item.get("title"),
                    duration=item.get("duration", 0),
                    artist=artist,
                    artists=[artist],
                    album=album,
                    explicit=item.get("parentalControls", {}).get("hasExplicitLanguage", False),
                    audioQuality="LOSSLESS",
                    isrc=item.get("isrc"),
                    trackNumber=int(item.get("trackNum", 1)) if item.get("trackNum") else None
                ))
            except Exception:
                continue
        return items

    def get_track_info(self, asin: str) -> TrackInfo:
        data = self._post("track", {"asin": asin, "tier": "best", "country": "US"})
        meta = data.get("metadata", {})
        
        album = Album(id=meta.get("album", ""), title=meta.get("album", "Unknown"), cover=meta.get("cover"))
        artist = Artist(id=meta.get("artist", ""), name=meta.get("artist", "Unknown"))
        
        return TrackInfo(
            id=meta.get("asin", asin),
            title=meta.get("title", ""),
            duration=0,
            artist=artist,
            artists=[artist],
            album=album,
            explicit=meta.get("is_explicit", False),
            audioQuality="LOSSLESS",
            isrc=meta.get("isrc"),
            copyright=meta.get("copyright"),
            releaseDate=meta.get("date"),
            trackNumber=int(meta.get("track_number", 1)) if meta.get("track_number") else None
        )

    def get_track_manifest(self, asin: str, quality: str = "LOSSLESS") -> Dict[str, Any]:
        data = self._post("track", {"asin": asin, "tier": "best", "country": "US"})
        stream_url = data.get("stream", {}).get("url")
        if not stream_url:
            raise Exception(f"Could not fetch a valid stream for track {asin}")
            
        full_url = f"https://amz.redsleaks.to{stream_url}"
        return {
            "data": {
                "manifestMimeType": "direct",
                "url": full_url
            }
        }
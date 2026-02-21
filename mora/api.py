import time
import requests
from typing import Optional, List, Dict, Any
from .models import TrackSearchItem, AlbumInfo, ArtistInfo, TrackInfo

class APIClient:
    def __init__(self, timeout: int = 30, retries: int = 3):
        """Initialize API client with endpoints, headers, and retry settings."""
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
        self.track_bases = ["https://hund.qqdl.site", "https://katze.qqdl.site"]

    def _get(self, url: str, params: Optional[Dict] = None) -> Dict[str, Any]:
        """Perform a GET request with retries and exponential backoff."""
        for attempt in range(self.retries):
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.RequestException:
                if attempt == self.retries - 1:
                    raise
                time.sleep(2 ** attempt)

    def search_tracks(self, query: str) -> List[TrackSearchItem]:
        """Search for tracks by name."""
        url = f"{self.search_base}/search/"
        data = self._get(url, {"s": query})
        items = data.get("data", {}).get("items", [])
        return [TrackSearchItem(**i) for i in items]

    def get_album(self, album_id: int) -> AlbumInfo:
        """Fetch album details and its tracks."""
        url = f"{self.album_base}/album/"
        data = self._get(url, {"id": album_id})
        album_data = data.get("data", {})
        items = []
        for it in album_data.get("items", []):
            if "item" in it:
                items.append(it["item"])
        album_data["items"] = items
        return AlbumInfo(**album_data)

    def get_track_info(self, track_id: int) -> TrackInfo:
        """Fetch detailed metadata for a specific track."""
        url = f"{self.info_base}/info/"
        data = self._get(url, {"id": track_id})
        return TrackInfo(**data.get("data", {}))

    def get_artist(self, artist_id: int) -> ArtistInfo:
        """Fetch artist details including albums and tracks."""
        url = f"{self.artist_base}/artist/"
        data = self._get(url, {"f": artist_id})
        albums = data.get("albums", {}).get("items", [])
        tracks_data = data.get("tracks", [])
        
        normalized_tracks = []
        for t in tracks_data:
            if 'artist' not in t and 'artists' in t and t['artists']:
                t['artist'] = t['artists'][0]
            normalized_tracks.append(t)
            
        tracks = [TrackSearchItem(**t) for t in normalized_tracks]
        return ArtistInfo(albums=albums, tracks=tracks)

    def get_track_manifest(self, track_id: int, quality: str = "LOSSLESS") -> Dict[str, Any]:
        """Fetch the download manifest for a track trying multiple base URLs."""
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
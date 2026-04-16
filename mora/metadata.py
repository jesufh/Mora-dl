from concurrent.futures import ThreadPoolExecutor
from typing import List

import requests

from .models import Album, Artist, Track


class DeezerProvider:
    BASE_URL = "https://api.deezer.com"

    def __init__(self):
        self.session = requests.Session()

    def _enrich_tracks(self, data: List[dict]) -> List[dict]:
        def fetch_extra(item):
            if "contributors" not in item:
                try:
                    response = self.session.get(f"{self.BASE_URL}/track/{item['id']}")
                    if response.status_code == 200:
                        item["contributors"] = response.json().get("contributors", [])
                except Exception:
                    pass
            return item

        with ThreadPoolExecutor(max_workers=8) as executor:
            return list(executor.map(fetch_extra, data))

    def _get_artist_name(self, data_dict: dict, fallback: str = "Unknown") -> str:
        contributors = data_dict.get("contributors")
        if contributors:
            return ", ".join(contributor.get("name") for contributor in contributors)
        if "artist" in data_dict and "name" in data_dict["artist"]:
            return data_dict["artist"]["name"]
        return fallback

    def search_tracks(self, query: str, limit: int = 20) -> List[Track]:
        response = self.session.get(f"{self.BASE_URL}/search", params={"q": query, "limit": limit})
        response.raise_for_status()
        data = self._enrich_tracks(response.json().get("data", []))

        return [
            Track(
                id=str(item["id"]),
                title=item["title"],
                artist=self._get_artist_name(item),
                album=item["album"]["title"],
                duration=item["duration"],
                cover_url=item["album"].get("cover_xl"),
            )
            for item in data
        ]

    def search_albums(self, query: str, limit: int = 15) -> List[Album]:
        response = self.session.get(f"{self.BASE_URL}/search/album", params={"q": query, "limit": limit})
        response.raise_for_status()
        return [
            Album(
                id=str(item["id"]),
                title=item["title"],
                artist=self._get_artist_name(item),
                track_count=item.get("nb_tracks", 0),
                cover_url=item.get("cover_xl"),
            )
            for item in response.json().get("data", [])
        ]

    def get_album_tracks(self, album: Album) -> List[Track]:
        response = self.session.get(f"{self.BASE_URL}/album/{album.id}")
        response.raise_for_status()
        data = response.json()
        tracks_data = self._enrich_tracks(data.get("tracks", {}).get("data", []))

        return [
            Track(
                id=str(item["id"]),
                title=item["title"],
                artist=self._get_artist_name(item, fallback=album.artist),
                album=album.title,
                duration=item["duration"],
                cover_url=album.cover_url,
                track_number=item.get("track_position"),
                disc_number=item.get("disk_number"),
            )
            for item in tracks_data
        ]

    def search_artists(self, query: str, limit: int = 10) -> List[Artist]:
        response = self.session.get(f"{self.BASE_URL}/search/artist", params={"q": query, "limit": limit})
        response.raise_for_status()
        return [
            Artist(
                id=str(item["id"]),
                name=item["name"],
                fan_count=item.get("nb_fan", 0),
            )
            for item in response.json().get("data", [])
        ]

    def get_artist_top_tracks(self, artist: Artist, limit: int = 50) -> List[Track]:
        response = self.session.get(f"{self.BASE_URL}/artist/{artist.id}/top", params={"limit": limit})
        response.raise_for_status()
        data = self._enrich_tracks(response.json().get("data", []))

        return [
            Track(
                id=str(item["id"]),
                title=item["title"],
                artist=self._get_artist_name(item, fallback=artist.name),
                album=item["album"]["title"],
                duration=item["duration"],
                cover_url=item["album"].get("cover_xl"),
            )
            for item in data
        ]

    def get_track_details(self, track: Track) -> Track:
        response = self.session.get(f"{self.BASE_URL}/track/{track.id}")
        if response.status_code == 200:
            data = response.json()
            track.isrc = data.get("isrc")
            track.release_date = data.get("release_date")
            track.track_number = data.get("track_position")
            track.disc_number = data.get("disk_number")
            track.title = data.get("title", track.title)
            track.artist = self._get_artist_name(data, fallback=track.artist)
            track.album = data.get("album", {}).get("title", track.album)

            album_id = data.get("album", {}).get("id")
            if album_id:
                album_response = self.session.get(f"{self.BASE_URL}/album/{album_id}")
                if album_response.status_code == 200:
                    album_data = album_response.json()
                    track.copyright = album_data.get("copyright", "")
                    genres = album_data.get("genres", {}).get("data", [])
                    if genres:
                        track.genre = genres[0].get("name")
        return track

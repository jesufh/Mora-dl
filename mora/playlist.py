import json
import re
from typing import List, Tuple

import requests


class SpotifyExtractor:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
                )
            }
        )

    def extract_tracks(self, url: str) -> Tuple[str, List[dict]]:
        match = re.search(r"playlist/([a-zA-Z0-9]+)", url)
        if not match:
            raise ValueError("Invalid Spotify playlist URL.")

        playlist_id = match.group(1)
        embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
        response = self.session.get(embed_url)
        response.raise_for_status()

        data_match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            response.text,
            re.DOTALL,
        )
        if not data_match:
            raise Exception("Could not extract playlist information. The playlist may be private.")

        data = json.loads(data_match.group(1))

        try:
            entity = data["props"]["pageProps"]["state"]["data"]["entity"]
            playlist_name = entity.get("name", "Spotify Playlist")
            track_list = entity.get("trackList", [])

            tracks = []
            for item in track_list:
                title = item.get("title", "")
                artist = item.get("subtitle", "")
                if not artist:
                    artist = " ".join(artist_item.get("name", "") for artist_item in item.get("artists", []))

                if title and artist:
                    tracks.append({"title": title, "artist": artist})

            return playlist_name, tracks
        except KeyError:
            raise Exception("Unrecognized Spotify response structure.")

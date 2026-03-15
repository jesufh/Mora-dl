import re
import requests
import yt_dlp
from typing import List, Dict, Tuple

class PlaylistExtractor:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })

    def get_tracks(self, url: str) -> Tuple[str, List[Dict[str, str]]]:
        if "spotify.com" in url:
            return self._get_spotify(url)
        return self._get_ytdlp(url)

    def _get_spotify(self, url: str) -> Tuple[str, List[Dict[str, str]]]:
        match = re.search(r"playlist/([a-zA-Z0-9]+)", url)
        if not match:
            return "Unknown Playlist",[]
        
        pid = match.group(1)
        token_url = "https://open.spotify.com/get_access_token?reason=transport&productType=web_player"
        token = self.session.get(token_url).json().get("accessToken")
        
        self.session.headers.update({"Authorization": f"Bearer {token}"})
        
        info_url = f"https://api.spotify.com/v1/playlists/{pid}"
        info_resp = self.session.get(info_url).json()
        playlist_name = info_resp.get("name", "Spotify Playlist")
        
        api_url = f"https://api.spotify.com/v1/playlists/{pid}/tracks"
        tracks =[]
        
        while api_url:
            resp = self.session.get(api_url).json()
            for item in resp.get("items",[]):
                track = item.get("track")
                if track:
                    title = track.get("name")
                    artists = " ".join([a.get("name") for a in track.get("artists", [])])
                    tracks.append({"title": title, "artist": artists})
            api_url = resp.get("next")
            
        return playlist_name, tracks

    def _get_ytdlp(self, url: str) -> Tuple[str, List[Dict[str, str]]]:
        opts = {'extract_flat': True, 'quiet': True}
        tracks =[]
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            playlist_name = info.get('title', 'Playlist')
            entries = info.get('entries', [info])
            for e in entries:
                if e:
                    title = e.get('title', '')
                    artist = e.get('uploader', '') or e.get('channel', '')
                    tracks.append({"title": title, "artist": artist})
        return playlist_name, tracks
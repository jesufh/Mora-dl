import re
import json
import requests
import yt_dlp
from typing import List, Dict, Tuple

class PlaylistExtractor:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5"
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
        
        # we used the Spotify embed
        # this doesn't have strong anti-bot protections
        embed_url = f"https://open.spotify.com/embed/playlist/{pid}"
        
        page_resp = self.session.get(embed_url)
        page_resp.raise_for_status()
        
        # we extract the React state in the html
        next_data_match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>', page_resp.text, re.DOTALL)
        if not next_data_match:
            raise Exception("Could not find playlist data in Spotify embed page. Layout might have changed.")
            
        try:
            data = json.loads(next_data_match.group(1))
        except json.JSONDecodeError:
            raise Exception("Failed to parse Spotify embed data.")
            
        token = None
        try:
            # then we extract the anonymous access token
            token = data.get("props", {}).get("pageProps", {}).get("state", {}).get("session", {}).get("accessToken")
        except Exception:
            pass

        # if work, we use the official API
        if token:
            auth_headers = {"Authorization": f"Bearer {token}"}
            info_url = f"https://api.spotify.com/v1/playlists/{pid}"
            
            info_resp = self.session.get(info_url, headers=auth_headers)
            if info_resp.status_code == 200:
                info_json = info_resp.json()
                playlist_name = info_json.get("name", "Spotify Playlist")
                
                api_url = f"https://api.spotify.com/v1/playlists/{pid}/tracks"
                tracks =[]
                
                while api_url:
                    resp = self.session.get(api_url, headers=auth_headers)
                    if resp.status_code != 200:
                        break
                    resp_json = resp.json()
                    
                    for item in resp_json.get("items",[]):
                        track = item.get("track")
                        if track:
                            title = track.get("name")
                            artists = " ".join([a.get("name") for a in track.get("artists", [])])
                            tracks.append({"title": title, "artist": artists})
                            
                    api_url = resp_json.get("next")
                    
                if tracks:
                    return playlist_name, tracks

        # if for any reason the API fails or there is no token, 
        # we perform a fallback by extracting directly from the embed JSON
        try:
            entity = data["props"]["pageProps"]["state"]["data"]["entity"]
            playlist_name = entity.get("name", "Spotify Playlist")
            track_list = entity.get("trackList", [])
            
            tracks =[]
            for item in track_list:
                title = item.get("title", "")
                artist = item.get("subtitle", "")
                if not artist and "artists" in item:
                    artist = " ".join([a.get("name", "") for a in item["artists"]])
                
                if title:
                    tracks.append({"title": title, "artist": artist})
                    
            return playlist_name, tracks
        except KeyError as e:
            raise Exception(f"Unexpected data structure in Spotify embed fallback: missing key {e}")

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

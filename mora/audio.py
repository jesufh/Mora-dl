import base64
import json
from typing import Optional

import requests


class TidalProvider:
    HOSTS = [
        "https://katze.qqdl.site",
        "https://hund.qqdl.site",
        "https://triton.squid.wtf",
        "https://api.monochrome.tf",
    ]

    def __init__(self):
        self.session = requests.Session()

    def _request(self, endpoint: str, params: dict) -> dict:
        for host in self.HOSTS:
            try:
                response = self.session.get(f"{host}{endpoint}", params=params, timeout=10)
                response.raise_for_status()
                return response.json()
            except requests.RequestException:
                continue
        raise Exception(f"All audio servers failed for: {endpoint}")

    def find_track_id(self, title: str, artist: str, target_duration: int) -> Optional[int]:
        main_artist = artist.split(",")[0].strip()
        query = f"{title} {main_artist}"
        data = self._request("/search/", {"s": query})
        items = data.get("data", {}).get("items", [])

        for item in items:
            duration = item.get("duration", 0)
            if abs(duration - target_duration) <= 4:
                return item.get("id") or item.get("trackId")

        if items:
            return items[0].get("id") or items[0].get("trackId")
        return None

    def get_stream_manifest(self, track_id: int, quality: str = "HI_RES_LOSSLESS") -> dict:
        data = self._request("/track/", {"id": track_id, "quality": quality})
        track_data = data.get("data", {})

        mime = track_data.get("manifestMimeType")
        manifest_b64 = track_data.get("manifest")

        if not manifest_b64:
            raise Exception("The track is not available for download or is blocked.")

        manifest_raw = base64.b64decode(manifest_b64)
        result = {
            "mime": mime,
            "quality": track_data.get("audioQuality"),
            "bitDepth": track_data.get("bitDepth", 16),
            "sampleRate": track_data.get("sampleRate", 44100),
        }

        if mime == "application/vnd.tidal.bts":
            manifest = json.loads(manifest_raw)
            result["urls"] = manifest.get("urls", [])
        elif mime == "application/dash+xml":
            result["dash_xml"] = manifest_raw.decode("utf-8")
        else:
            raise Exception(f"Unsupported manifest format: {mime}")

        return result

import os
import base64
import json
import xml.etree.ElementTree as ET
import subprocess
from urllib.parse import urljoin
from typing import Union
import requests
from tqdm import tqdm
from .models import TrackInfo

class Downloader:
    def __init__(self, output_dir: str = "downloads"):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })

    def _download_file(self, url: str, dest: str, desc: str = None, silent: bool = False) -> None:
        resp = self.session.get(url, stream=True)
        resp.raise_for_status()
        
        if silent:
            with open(dest, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)
            return

        total = int(resp.headers.get("content-length", 0))
        with open(dest, "wb") as f, tqdm(
            desc=desc or os.path.basename(dest),
            total=total,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
        ) as pbar:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)
                pbar.update(len(chunk))

    def download_track(self, track_id: Union[int, str], manifest_data: dict, metadata: TrackInfo) -> str:
        inner = manifest_data.get("data", {})
        mime = inner.get("manifestMimeType")
        
        if mime == "direct":
            url = inner.get("url")
            if not url:
                raise ValueError(f"Direct URL is empty for track {track_id}")
            return self._download_direct(url, metadata)

        manifest_b64 = inner.get("manifest")
        if not manifest_b64:
            raise ValueError(f"Manifest is empty for track {track_id}")

        manifest_bytes = base64.b64decode(manifest_b64)

        if mime == "application/vnd.tidal.bts":
            return self._download_bts(manifest_bytes, metadata)
        elif mime == "application/dash+xml":
            return self._download_dash(manifest_bytes, metadata)
        else:
            raise ValueError(f"Unsupported manifest type: {mime}")

    def _download_direct(self, url: str, metadata: TrackInfo) -> str:
        artist_names = ", ".join([a.name for a in metadata.artists if a.name])
        filename = self._sanitize_filename(f"{artist_names} - {metadata.title}.flac")
        path = os.path.join(self.output_dir, filename)
        self._download_file(url, path, desc=metadata.title)
        return path

    def _download_bts(self, manifest_bytes: bytes, metadata: TrackInfo) -> str:
        data = json.loads(manifest_bytes)
        urls = data.get("urls")
        if not urls:
            raise ValueError("No URLs found in BTS manifest")
        url = urls[0]

        artist_names = ", ".join([a.name for a in metadata.artists if a.name])
        filename = self._sanitize_filename(f"{artist_names} - {metadata.title}.flac")
        path = os.path.join(self.output_dir, filename)
        self._download_file(url, path, desc=metadata.title)
        return path

    def _get_ffmpeg_path(self) -> str:
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            return "ffmpeg"

    def _download_dash(self, manifest_xml: bytes, metadata: TrackInfo) -> str:
        root = ET.fromstring(manifest_xml)
        ns = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}

        base_url_elem = root.find(".//mpd:BaseURL", ns) or root.find(".//BaseURL")
        base_url = base_url_elem.text if base_url_elem is not None else ""

        representation = root.find(".//mpd:Representation", ns) or root.find(".//Representation")
        if representation is None:
            raise ValueError("Could not find Representation in MPD")

        seg_template = representation.find(".//mpd:SegmentTemplate", ns) or representation.find(".//SegmentTemplate")
        if seg_template is None:
            raise ValueError("Could not find SegmentTemplate")

        init_template = seg_template.get("initialization")
        media_template = seg_template.get("media")
        if not init_template or not media_template:
            raise ValueError("Missing attributes in SegmentTemplate")

        timeline = seg_template.find(".//mpd:SegmentTimeline", ns) or seg_template.find(".//SegmentTimeline")
        
        artist_names = ", ".join([a.name for a in metadata.artists if a.name])
        filename = self._sanitize_filename(f"{artist_names} - {metadata.title}.flac")
        out_path = os.path.join(self.output_dir, filename)

        if timeline is None:
            media_url = urljoin(base_url, media_template.replace("$Number$", "1"))
            self._download_file(media_url, out_path, desc=metadata.title)
            return out_path

        segments =[]
        for s in timeline.findall(".//mpd:S", ns) or timeline.findall("S"):
            d = int(s.get("d"))
            r = int(s.get("r", 0)) + 1
            segments.extend([d] * r)

        init_url = urljoin(base_url, init_template)
        init_path = os.path.join(self.output_dir, f"{metadata.id}_init.m4s")
        self._download_file(init_url, init_path, silent=True)

        media_paths =[]
        with tqdm(total=len(segments), desc=metadata.title, unit="seg") as pbar:
            for i in range(1, len(segments) + 1):
                seg_url = urljoin(base_url, media_template.replace("$Number$", str(i)))
                seg_path = os.path.join(self.output_dir, f"{metadata.id}_seg_{i}.m4s")
                self._download_file(seg_url, seg_path, silent=True)
                media_paths.append(seg_path)
                pbar.update(1)

        temp_mp4 = os.path.join(self.output_dir, f"{metadata.id}_temp.mp4")

        with open(temp_mp4, "wb") as out:
            with open(init_path, "rb") as f:
                out.write(f.read())
            for mp in media_paths:
                with open(mp, "rb") as f:
                    out.write(f.read())

        os.remove(init_path)
        for mp in media_paths:
            os.remove(mp)

        ffmpeg_cmd = self._get_ffmpeg_path()

        try:
            subprocess.run([
                ffmpeg_cmd, "-y", "-i", temp_mp4, "-c:a", "copy", out_path
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except FileNotFoundError:
            raise Exception("FFmpeg is not available. Install 'imageio-ffmpeg' (pip install imageio-ffmpeg) or install FFmpeg on your system.")
        except subprocess.CalledProcessError as e:
            raise Exception(f"Error processing DASH file with FFmpeg: {e}")
        finally:
            if os.path.exists(temp_mp4):
                os.remove(temp_mp4)

        return out_path

    def _sanitize_filename(self, name: str) -> str:
        import re
        return re.sub(r'[\\/*?:"<>|]', "", name).strip()
import os
import re
import base64
import json
import logging
import xml.etree.ElementTree as ET
import subprocess
from urllib.parse import urljoin
from typing import Union
import requests

logger = logging.getLogger(__name__)
from tqdm import tqdm
from .models import TrackInfo

class DownloadInterrupted(Exception):
    def __init__(self, path: str, message: str = "Download interrupted"):
        super().__init__(message)
        self.path = path

class Downloader:
    def __init__(self, output_dir: str = "downloads"):
        self.output_dir = output_dir
        os.makedirs(output_dir, exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })

    def _download_file(self, url: str, dest: str, desc: str = None, silent: bool = False) -> None:
        existing_size = os.path.getsize(dest) if os.path.exists(dest) else 0
        headers = {}
        if existing_size > 0:
            headers["Range"] = f"bytes={existing_size}-"

        try:
            response = self.session.get(url, stream=True, headers=headers)
        except requests.RequestException as exc:
            raise DownloadInterrupted(dest, f"Download interrupted for {dest}") from exc

        with response as resp:
            resume = existing_size > 0 and resp.status_code == 206
            if existing_size > 0 and resp.status_code == 200:
                existing_size = 0
                resume = False
            resp.raise_for_status()

            total = 0
            content_range = resp.headers.get("content-range")
            if content_range and "/" in content_range:
                total_part = content_range.rsplit("/", 1)[-1]
                if total_part.isdigit():
                    total = int(total_part)
            elif resp.headers.get("content-length"):
                try:
                    total = int(resp.headers.get("content-length", 0)) + existing_size
                except ValueError:
                    total = 0

            mode = "ab" if resume else "wb"
            try:
                with open(dest, mode) as f:
                    if silent:
                        for chunk in resp.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                        return

                    with tqdm(
                        desc=desc or os.path.basename(dest),
                        total=total if total > 0 else None,
                        initial=existing_size if total > 0 else 0,
                        unit="B",
                        unit_scale=True,
                        unit_divisor=1024,
                    ) as pbar:
                        for chunk in resp.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                                pbar.update(len(chunk))
            except (requests.RequestException, OSError) as exc:
                raise DownloadInterrupted(dest, f"Download interrupted for {dest}") from exc

    def download_track(self, track_id: Union[int, str], manifest_data: dict, metadata: TrackInfo) -> str:
        inner = manifest_data.get("data", {})
        mime = inner.get("manifestMimeType")
        
        if mime == "direct":
            url = inner.get("url")
            if not url:
                raise ValueError(f"Direct URL is empty for track {track_id}")
            drm_key = inner.get("drm_key")
            return self._download_direct(url, metadata, drm_key)

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

    def _get_ffmpeg_path(self) -> str:
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            return "ffmpeg"

    def _download_direct(self, url: str, metadata: TrackInfo, drm_key: str = None) -> str:
        artist_names = ", ".join([a.name for a in metadata.artists if a.name])
        filename = self._sanitize_filename(f"{artist_names} - {metadata.title}.flac")
        final_path = os.path.join(self.output_dir, filename)
        
        if drm_key:
            # Download into an encrypted temporary MP4 file.
            temp_path = os.path.join(self.output_dir, f"{metadata.id}_encrypted.mp4")
            self._download_file(url, temp_path, desc=metadata.title)
            
            ffmpeg_cmd = self._get_ffmpeg_path()
            try:
                # Use FFmpeg to decrypt CENC and extract FLAC without re-encoding (-c:a copy).
                subprocess.run([
                    ffmpeg_cmd, "-y", 
                    "-decryption_key", drm_key, 
                    "-i", temp_path, 
                    "-c:a", "copy", 
                    final_path
                ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except FileNotFoundError:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise Exception("FFmpeg is not available. Install 'imageio-ffmpeg' (pip install imageio-ffmpeg) or install FFmpeg on your system.")
            except subprocess.CalledProcessError as e:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
                raise Exception(f"Error decrypting and extracting FLAC with FFmpeg: {e}")
                
            # Remove temporary file after decryption/conversion.
            if os.path.exists(temp_path):
                os.remove(temp_path)
        else:
            # If there is no DRM key, assume the stream is raw FLAC.
            self._download_file(url, final_path, desc=metadata.title)
            
        return final_path

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

        segment_count = 0
        for s in timeline.findall(".//mpd:S", ns) or timeline.findall("S"):
            repeat = int(s.get("r", 0)) + 1
            segment_count += repeat

        if segment_count == 0:
            raise ValueError("No segments found in SegmentTimeline")

        init_url = urljoin(base_url, init_template)
        init_path = os.path.join(self.output_dir, f"{metadata.id}_init.m4s")
        self._download_file(init_url, init_path, silent=True)

        temp_mp4 = os.path.join(self.output_dir, f"{metadata.id}_temp.mp4")
        state_path = f"{temp_mp4}.state"
        ffmpeg_cmd = self._get_ffmpeg_path()
        completed = False
        resume_from = 1

        if os.path.exists(temp_mp4) and os.path.exists(state_path):
            try:
                with open(state_path, "r", encoding="utf-8") as state_file:
                    last_segment = int(state_file.read().strip() or "0")
                resume_from = max(1, last_segment + 1)
            except (OSError, ValueError):
                resume_from = 1
        elif os.path.exists(temp_mp4):
            try:
                os.remove(temp_mp4)
            except OSError:
                pass

        try:
            with open(temp_mp4, "ab" if resume_from > 1 else "wb") as out:
                with open(init_path, "rb") as f:
                    if resume_from == 1:
                        out.write(f.read())

                with tqdm(total=segment_count, desc=metadata.title, unit="seg") as pbar:
                    if resume_from > 1:
                        pbar.update(resume_from - 1)

                    for i in range(resume_from, segment_count + 1):
                        seg_url = urljoin(base_url, media_template.replace("$Number$", str(i)))
                        try:
                            with self.session.get(seg_url, stream=True) as seg_resp:
                                seg_resp.raise_for_status()
                                for chunk in seg_resp.iter_content(chunk_size=8192):
                                    if chunk:
                                        out.write(chunk)
                        except requests.RequestException as exc:
                            try:
                                with open(state_path, "w", encoding="utf-8") as state_file:
                                    state_file.write(str(i - 1))
                            except OSError:
                                pass
                            raise DownloadInterrupted(temp_mp4, f"Download interrupted for {metadata.title}") from exc
                        try:
                            with open(state_path, "w", encoding="utf-8") as state_file:
                                state_file.write(str(i))
                        except OSError:
                            pass
                        pbar.update(1)

            subprocess.run([
                ffmpeg_cmd, "-y", "-i", temp_mp4, "-c:a", "copy", out_path
            ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            completed = True
        except FileNotFoundError:
            raise Exception("FFmpeg is not available. Install 'imageio-ffmpeg' (pip install imageio-ffmpeg) or install FFmpeg on your system.")
        except subprocess.CalledProcessError as e:
            raise Exception(f"Error processing DASH file with FFmpeg: {e}")
        finally:
            if os.path.exists(init_path):
                os.remove(init_path)
            if completed and os.path.exists(temp_mp4):
                os.remove(temp_mp4)
            if completed and os.path.exists(state_path):
                os.remove(state_path)

        return out_path

    def _sanitize_filename(self, name: str) -> str:
        return re.sub(r'[\\/*?:"<>|]', "", name).strip()

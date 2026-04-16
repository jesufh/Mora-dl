import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from mutagen.flac import FLAC, Picture
from tqdm import tqdm

from .models import Track


class AudioDownloader:
    def __init__(self, output_dir: str = "./downloads"):
        self.output_dir = output_dir
        self.session = requests.Session()
        os.makedirs(output_dir, exist_ok=True)

    def _sanitize(self, filename: str) -> str:
        return "".join(char for char in filename if char.isalnum() or char in " ._-(),").rstrip()

    def download_and_tag(self, track: Track, manifest: dict):
        filename = self._sanitize(f"{track.artist} - {track.title}.flac")
        output_path = os.path.join(self.output_dir, filename)
        temp_path = output_path + ".tmp.mp4"
        mime = manifest["mime"]

        try:
            if mime == "application/vnd.tidal.bts":
                self._download_direct(manifest["urls"][0], output_path, track.title)
            elif mime == "application/dash+xml":
                self._download_dash(manifest["dash_xml"], temp_path, output_path, track.title)

            self._write_metadata(output_path, track)
            return output_path
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def _download_direct(self, url: str, output_path: str, desc: str):
        response = self.session.get(url, stream=True)
        response.raise_for_status()
        total = int(response.headers.get("content-length", 0))

        with open(output_path, "wb") as handle, tqdm(
            desc=desc,
            total=total,
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
        ) as bar:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    handle.write(chunk)
                    bar.update(len(chunk))

    def _download_dash(self, xml_str: str, temp_path: str, output_path: str, desc: str):
        root = ET.fromstring(xml_str)
        ns = {"mpd": "urn:mpeg:dash:schema:mpd:2011"}

        rep = root.find(".//mpd:Representation", ns)
        if rep is None:
            rep = root.find(".//Representation")

        template = rep.find(".//mpd:SegmentTemplate", ns) or rep.find(".//SegmentTemplate")
        init_url = template.get("initialization")
        media_url = template.get("media")
        timeline = template.find(".//mpd:SegmentTimeline", ns) or template.find(".//SegmentTimeline")

        segments = 0
        for segment in timeline.findall(".//mpd:S", ns) or timeline.findall(".//S"):
            segments += int(segment.get("r", 0)) + 1

        temp_dir = temp_path + "_dir"
        os.makedirs(temp_dir, exist_ok=True)

        try:
            self._download_file_silent(init_url, os.path.join(temp_dir, "init.mp4"))

            def download_segment(number):
                url = media_url.replace("$Number$", str(number))
                path = os.path.join(temp_dir, f"{number}.m4s")
                self._download_file_silent(url, path)
                return number

            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = [executor.submit(download_segment, i) for i in range(1, segments + 1)]
                for _ in tqdm(as_completed(futures), total=segments, desc=desc, unit="seg"):
                    pass

            with open(temp_path, "wb") as outfile:
                with open(os.path.join(temp_dir, "init.mp4"), "rb") as infile:
                    outfile.write(infile.read())
                for i in range(1, segments + 1):
                    with open(os.path.join(temp_dir, f"{i}.m4s"), "rb") as infile:
                        outfile.write(infile.read())

            self._remux_to_flac(temp_path, output_path)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _download_file_silent(self, url: str, path: str):
        response = self.session.get(url)
        response.raise_for_status()
        with open(path, "wb") as handle:
            handle.write(response.content)

    def _remux_to_flac(self, input_mp4: str, output_flac: str):
        if not shutil.which("ffmpeg"):
            raise Exception("FFmpeg is not installed on this system.")
        command = ["ffmpeg", "-y", "-i", input_mp4, "-c:a", "copy", output_flac]
        subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)

    def _write_metadata(self, filepath: str, track: Track):
        audio = FLAC(filepath)
        audio.delete()
        audio["TITLE"] = track.title
        audio["ARTIST"] = track.artist
        audio["ALBUM"] = track.album
        if track.track_number:
            audio["TRACKNUMBER"] = str(track.track_number)
        if track.disc_number:
            audio["DISCNUMBER"] = str(track.disc_number)
        if track.release_date:
            audio["DATE"] = str(track.release_date)[:4]
        if track.isrc:
            audio["ISRC"] = track.isrc
        if track.copyright:
            audio["COPYRIGHT"] = track.copyright
        if track.genre:
            audio["GENRE"] = track.genre

        if track.cover_url:
            try:
                cover_data = self.session.get(track.cover_url).content
                picture = Picture()
                picture.type = 3
                picture.mime = "image/jpeg"
                picture.desc = "Front Cover"
                picture.data = cover_data
                audio.add_picture(picture)
            except Exception:
                pass

        audio.save()

import requests
import logging
from mutagen.flac import FLAC, Picture
from .models import TrackInfo

logger = logging.getLogger(__name__)

class MetadataWriter:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
            "Referer": "https://tidal.com/",
            "Origin": "https://tidal.com",
            "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
        })

    def download_cover(self, cover_id: str) -> bytes | None:
        if not cover_id:
            return None

        if cover_id.startswith("http"):
            try:
                resp = self.session.get(cover_id, timeout=15)
                resp.raise_for_status()
                return resp.content
            except requests.RequestException as e:
                logger.error(f"Error downloading cover {cover_id}: {e}")
                return None

        clean_uuid = cover_id.replace("-", "")
        if len(clean_uuid) != 32:
            logger.error(f"Invalid cover UUID: {cover_id}")
            return None

        parts =[
            clean_uuid[0:8], clean_uuid[8:12], clean_uuid[12:16],
            clean_uuid[16:20], clean_uuid[20:32]
        ]
        path = "/".join(parts)

        url = f"https://resources.tidal.com/images/{path}/1280x1280.jpg"
        try:
            resp = self.session.get(url, timeout=15)
            resp.raise_for_status()
            return resp.content
        except requests.RequestException as e:
            logger.error(f"Error downloading cover {url}: {e}")
            return None

    def write_flac(self, filepath: str, metadata: TrackInfo, cover_data: bytes | None = None) -> None:
        try:
            audio = FLAC(filepath)
        except Exception as e:
            logger.error(f"Could not open FLAC for metadata processing: {e}")
            return

        audio.delete()

        if metadata.title: 
            audio["TITLE"] = str(metadata.title)

        artist_names = [a.name for a in metadata.artists if a.name]
        if artist_names:
            audio["ARTIST"] = ", ".join(artist_names)

        if metadata.artist and metadata.artist.name:
            audio["ALBUMARTIST"] = str(metadata.artist.name)

        if metadata.album and metadata.album.title:
            audio["ALBUM"] = str(metadata.album.title)

        if metadata.trackNumber is not None:
            audio["TRACKNUMBER"] = str(metadata.trackNumber)

        year = None
        if metadata.releaseDate:
            year = str(metadata.releaseDate[:4])
        elif metadata.streamStartDate:
            year = str(metadata.streamStartDate[:4])
        
        if year:
            audio["DATE"] = year
            audio["YEAR"] = year

        if metadata.copyright: audio["COPYRIGHT"] = str(metadata.copyright)
        if metadata.isrc: audio["ISRC"] = str(metadata.isrc)
        if metadata.bpm is not None: audio["BPM"] = str(metadata.bpm)

        if cover_data:
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.desc = "Front Cover"
            pic.data = cover_data
            audio.clear_pictures()
            audio.add_picture(pic)

        audio.save()
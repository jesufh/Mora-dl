from pydantic import BaseModel
from typing import List, Optional

class Artist(BaseModel):
    id: int
    name: str
    picture: Optional[str] = None
    type: Optional[str] = None

class Album(BaseModel):
    id: int
    title: str
    cover: Optional[str] = None
    vibrantColor: Optional[str] = None
    artist: Optional[Artist] = None

class TrackSearchItem(BaseModel):
    id: int
    title: str
    duration: int
    popularity: Optional[int] = None
    artist: Artist
    artists: List[Artist]
    album: Album
    explicit: Optional[bool] = False
    audioQuality: Optional[str] = None
    version: Optional[str] = None
    isrc: Optional[str] = None
    copyright: Optional[str] = None
    bpm: Optional[int] = None
    key: Optional[str] = None
    keyScale: Optional[str] = None
    releaseDate: Optional[str] = None
    trackNumber: Optional[int] = None
    volumeNumber: Optional[int] = None
    streamStartDate: Optional[str] = None

class TrackInfo(TrackSearchItem):
    pass

class AlbumInfo(BaseModel):
    id: int
    title: str
    cover: Optional[str] = None
    artists: List[Artist]
    items: List[dict]
    numberOfTracks: int
    releaseDate: Optional[str] = None
    copyright: Optional[str] = None
    explicit: Optional[bool] = False

class ArtistInfo(BaseModel):
    albums: List[Album]
    tracks: List[TrackSearchItem]
from pydantic import BaseModel, Field
from typing import List, Optional, Union

class Artist(BaseModel):
    id: Union[int, str]
    name: str
    picture: Optional[str] = None
    type: Optional[str] = None
    area: Optional[str] = None
    disambiguation: Optional[str] = None
    provider: Optional[str] = None

class Album(BaseModel):
    id: Union[int, str]
    title: str
    cover: Optional[str] = None
    vibrantColor: Optional[str] = None
    artist: Optional[Artist] = None
    releaseDate: Optional[str] = None
    disambiguation: Optional[str] = None
    provider: Optional[str] = None

class TrackSearchItem(BaseModel):
    id: Union[int, str]
    title: str
    duration: int
    popularity: Optional[int] = None
    artist: Artist
    artists: List[Artist]
    album: Album
    explicit: Optional[bool] = False
    audioQuality: Optional[str] = None
    bitDepth: Optional[int] = None
    sampleRate: Optional[int] = None
    mediaMetadata: Optional[dict] = None
    version: Optional[str] = None
    isrc: Optional[str] = None
    copyright: Optional[str] = None
    genre: Optional[str] = None
    label: Optional[str] = None
    composer: Optional[str] = None
    bpm: Optional[int] = None
    key: Optional[str] = None
    keyScale: Optional[str] = None
    releaseDate: Optional[str] = None
    trackNumber: Optional[int] = None
    volumeNumber: Optional[int] = None
    trackTotal: Optional[int] = None
    discTotal: Optional[int] = None
    streamStartDate: Optional[str] = None
    provider: Optional[str] = None

class TrackInfo(TrackSearchItem):
    streamCodec: Optional[str] = None
    streamTier: Optional[str] = None
    resolvedQuality: Optional[str] = None

class AlbumCandidate(BaseModel):
    id: Union[int, str]
    title: str
    artist: Optional[Artist] = None
    cover: Optional[str] = None
    releaseDate: Optional[str] = None
    disambiguation: Optional[str] = None
    provider: str
    tracks: List[TrackSearchItem] = Field(default_factory=list)

class ArtistCandidate(BaseModel):
    id: Union[int, str]
    name: str
    picture: Optional[str] = None
    type: Optional[str] = None
    area: Optional[str] = None
    disambiguation: Optional[str] = None
    provider: str
    tracks: List[TrackSearchItem] = Field(default_factory=list)

class AlbumInfo(BaseModel):
    id: Union[int, str]
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

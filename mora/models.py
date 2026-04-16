from pydantic import BaseModel
from typing import Optional

class Track(BaseModel):
    id: str
    title: str
    artist: str
    album: str
    duration: int
    isrc: Optional[str] = None
    release_date: Optional[str] = None
    track_number: Optional[int] = None
    disc_number: Optional[int] = None
    cover_url: Optional[str] = None
    copyright: Optional[str] = None
    genre: Optional[str] = None
    
    @property
    def display_name(self) -> str:
        return f"{self.artist} - {self.title}"

class Album(BaseModel):
    id: str
    title: str
    artist: str
    track_count: int
    cover_url: Optional[str] = None

class Artist(BaseModel):
    id: str
    name: str
    fan_count: int
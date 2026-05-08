"""
State machine for the CD Player backend.
States: IDLE → LOADED → PLAYING → PAUSED → IDLE
"""
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, List
import threading


class CDState(str, Enum):
    IDLE = "idle"
    LOADED = "loaded"
    PLAYING = "playing"
    PAUSED = "paused"


@dataclass
class TrackInfo:
    number: int
    title: str
    duration: int  # seconds


@dataclass
class CDPlayerState:
    state: CDState = CDState.IDLE
    disc_id: Optional[str] = None
    album: Optional[str] = None
    artist: Optional[str] = None
    cover_url: Optional[str] = None
    cover_local_path: Optional[str] = None
    tracks: List[TrackInfo] = field(default_factory=list)
    track_number: int = 0
    track_title: Optional[str] = None
    elapsed: int = 0
    duration: int = 0
    alsa_device: str = "hw:0,0"
    audio_state: str = "default"  # usb_single | usb_multiple | no_usb | device_missing | default
    loading: bool = False
    buffering: bool = False

    def reset(self):
        self.state = CDState.IDLE
        self.disc_id = None
        self.album = None
        self.artist = None
        self.cover_url = None
        self.cover_local_path = None
        self.tracks = []
        self.track_number = 0
        self.track_title = None
        self.elapsed = 0
        self.duration = 0
        self.loading = False
        self.buffering = False

    def to_status_dict(self) -> dict:
        return {
            "state": self.state.value,
            "track_number": self.track_number,
            "track_title": self.track_title,
            "elapsed": self.elapsed,
            "duration": self.duration,
            "album": self.album,
            "artist": self.artist,
            "cover_url": self.cover_url,
            "disc_id": self.disc_id,
            "tracks_total": len(self.tracks),
            "loading": self.loading,
            "buffering": self.buffering,
            "alsa_device": self.alsa_device,
            "audio_state": self.audio_state,
        }


# Global singleton state with a lock for thread safety
_lock = threading.Lock()
player_state = CDPlayerState()


def get_state() -> CDPlayerState:
    return player_state


def with_lock(fn):
    """Decorator for state mutations that need the lock."""
    def wrapper(*args, **kwargs):
        with _lock:
            return fn(*args, **kwargs)
    return wrapper

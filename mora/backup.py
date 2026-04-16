import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .models import Track


def _unique(values: list[str]) -> list[str]:
    result = []
    seen = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _normalize_identity_part(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return " ".join(ascii_text.casefold().split())


def backup_contexts(track: "Track") -> list[str]:
    contexts = []
    if track.isrc:
        contexts.append(f"isrc:{track.isrc.strip().upper()}")

    artist = _normalize_identity_part(track.artist)
    title = _normalize_identity_part(track.title)
    contexts.append(f"meta:{artist}|{title}|{int(track.duration)}")

    if track.id:
        contexts.append(f"deezer:{track.id.strip()}")

    return _unique(contexts)


def primary_backup_context(track: "Track") -> str:
    return backup_contexts(track)[0]


def decryption_contexts(track: "Track") -> list[str]:
    contexts = backup_contexts(track)
    if track.id:
        contexts.append(track.id.strip())
    return _unique(contexts)


def search_tokens(track: "Track", crypto) -> list[str]:
    tokens = [crypto.storage_token(context) for context in backup_contexts(track)]
    if track.id:
        tokens.append(crypto.legacy_storage_token(track.id.strip()))
    return _unique(tokens)

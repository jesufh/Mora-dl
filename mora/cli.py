import os
import re
from typing import List

import click
from rich import box
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from .api import (
    APIClient,
    AmazonAPIClient,
    CatalogService,
    MusicBrainzMetadataProvider,
    _get_field,
    deduplicate_tracks,
    normalize_text,
)
from .downloader import DownloadInterrupted, Downloader
from .metadata import MetadataWriter
from .playlist import PlaylistExtractor

console = Console()


@click.command(help="Download tracks, albums, artists, or playlists based on a search query.")
@click.option("--track", is_flag=True, help="Search for specific tracks")
@click.option("--album", is_flag=True, help="Search for albums")
@click.option("--artist", is_flag=True, help="Search for artists")
@click.option("--playlist", is_flag=True, help="Download from a playlist URL (Spotify, YouTube, etc.)")
@click.option("--query", "-q", required=True, help="Search term/query or URL")
@click.option("--quality", type=click.Choice(["LOSSLESS", "HI_RES_LOSSLESS"]), default="HI_RES_LOSSLESS", help="Audio quality (16/24 bits)")
@click.option("--output", "-o", default="./downloads", help="Output directory path")
def cli(track, album, artist, playlist, query, quality, output):
    flags = [track, album, artist, playlist]
    if sum(flags) != 1:
        console.print("[red]You must specify exactly one of: --track, --album, --artist, --playlist[/red]")
        return

    service = CatalogService([APIClient(), AmazonAPIClient()], metadata_provider=MusicBrainzMetadataProvider())
    downloader = Downloader(output)
    writer = MetadataWriter()

    if track:
        _handle_track_search(service, query, quality, downloader, writer)
    elif album:
        _handle_album_search(service, query, quality, downloader, writer)
    elif artist:
        _handle_artist_search(service, query, quality, downloader, writer)
    elif playlist:
        _handle_playlist_download(service, query, quality, output, writer)


# ── Formatting helpers ──────────────────────────────────────────────

def format_artists(artists_data) -> str:
    primary_artist = None
    if hasattr(artists_data, "artists") or isinstance(artists_data, dict):
        primary_artist = _get_field(artists_data, "artist")
        artists_data = _get_field(artists_data, "artists", [])
    seen = set()
    artists_list = []
    for artist in artists_data or []:
        name = artist.name if hasattr(artist, "name") else artist.get("name", "")
        if not name:
            continue
        canonical = name.upper()
        if canonical in seen:
            continue
        seen.add(canonical)
        artists_list.append(name)
    if not artists_list and primary_artist is not None:
        name = primary_artist.name if hasattr(primary_artist, "name") else primary_artist.get("name", "")
        if name:
            artists_list.append(name)
    if not artists_list:
        return "Unknown"
    return ", ".join(artists_list[:3]) + ("..." if len(artists_list) > 3 else "")


def format_title(item) -> str:
    title = _get_field(item, "title", "Unknown Title")
    version = _get_field(item, "version", "")
    explicit = _get_field(item, "explicit", False)
    if version and version.lower() not in title.lower():
        title = f"{title} ({version})"
    if explicit:
        title = f"{title} 🅴"
    return title


def _quality_from_audio_fields(item) -> str:
    bit_depth = _get_field(item, "bitDepth")
    sample_rate = _get_field(item, "sampleRate")
    if bit_depth is not None and sample_rate is not None:
        try:
            if int(bit_depth) > 16 or int(sample_rate) > 44100:
                return "HI_RES_LOSSLESS"
            return "LOSSLESS"
        except (TypeError, ValueError):
            pass

    resolved = _get_field(item, "resolvedQuality")
    if resolved:
        return resolved

    quality = _get_field(item, "audioQuality")
    if quality:
        return quality

    codec = _get_field(item, "streamCodec") or _get_field(item, "codec")
    if codec == "flac":
        return "LOSSLESS"
    if codec == "aac":
        return "HIGH"
    return "N/A"


def _quality_label(item) -> str:
    quality = _quality_from_audio_fields(item)
    bit_depth = _get_field(item, "bitDepth")
    sample_rate = _get_field(item, "sampleRate")
    if quality in {"LOSSLESS", "HI_RES_LOSSLESS"} and bit_depth and sample_rate:
        return f"{quality} ({bit_depth}-bit/{sample_rate}Hz)"
    return quality


def _provider_label(provider: str | None) -> str:
    return {
        "primary": "TIDAL",
        "amazon": "AMZN",
        "musicbrainz_metadata": "MB",
    }.get(provider or "", provider or "UNKNOWN")


def _is_metadata_provider(provider: str | None) -> bool:
    return provider == "musicbrainz_metadata"


def _format_release_date(value: str | None) -> str:
    if not value:
        return "-"
    return value[:10]


def _format_disc_track(item) -> tuple[str, str]:
    disc = _get_field(item, "volumeNumber")
    track = _get_field(item, "trackNumber")
    return (str(disc or "-"), str(track or "-"))


def _display_text(value: str | None, fallback: str = "-") -> str:
    if value is None:
        return fallback
    text = str(value).strip()
    return text or fallback


def _album_label(item) -> str:
    album = _get_field(item, "album")
    if not album:
        return "-"
    return _display_text(_get_field(album, "title"))


def _format_duration(value: int | None) -> str:
    if not value or value < 0:
        return "-:--"
    mins, secs = divmod(int(value), 60)
    return f"{mins}:{secs:02d}"


# ── Search handlers ──────────────────────────────────────────────

def _handle_track_search(service, query, quality, downloader, writer):
    with console.status("[bold green]Searching for tracks..."):
        tracks = service.search_tracks(query)

    if not tracks:
        console.print("[red]No tracks found.[/red]")
        return

    table = _create_compact_table("Found Tracks", ["#", "Title", "Artist(s)", "Album", "Provider", "Quality", "Duration"])
    for index, track in enumerate(tracks, 1):
        table.add_row(
            str(index),
            format_title(track),
            format_artists(track),
            _album_label(track),
            _provider_label(track.provider),
            _quality_label(track),
            _format_duration(track.duration),
        )
    console.print(table)

    choice = Prompt.ask("Numbers to download (e.g., 1,3-5, all)", default="all")
    selected = tracks if choice.lower() == "all" else _parse_choice(choice, tracks)
    for track in selected:
        _download_track(track, quality, service, downloader, writer)


def _handle_album_search(service, query, quality, downloader, writer):
    with console.status("[bold green]Searching for albums..."):
        albums = service.search_albums(query)

    if not albums:
        console.print("[red]No albums found.[/red]")
        return

    metadata_albums = albums and _is_metadata_provider(albums[0].provider)
    columns = ["#", "Album", "Artist", "Date", "Edition", "MBID"] if metadata_albums else ["#", "Album", "Artist", "Provider", "Seed Tracks"]
    table = _create_compact_table("Found Albums", columns)
    for index, album in enumerate(albums, 1):
        if metadata_albums:
            table.add_row(
                str(index),
                _display_text(album.title),
                _display_text(album.artist.name if album.artist else None, "Various"),
                _format_release_date(album.releaseDate),
                album.disambiguation or "-",
                str(album.id),
            )
        else:
            table.add_row(
                str(index),
                _display_text(album.title),
                _display_text(album.artist.name if album.artist else None, "Various"),
                _provider_label(album.provider),
                str(len(album.tracks)),
            )
    console.print(table)

    choice = Prompt.ask("Album number to view tracks", default="1")
    try:
        album = albums[int(choice) - 1]
    except (ValueError, IndexError):
        console.print("[red]Invalid number[/red]")
        return

    with console.status(f"[bold green]Fetching tracks for {album.title}..."):
        tracks = service.get_album_tracks(album)

    if not tracks:
        console.print("[red]No tracks found in this album.[/red]")
        return

    _display_track_table(tracks, f"Tracks in {album.title}")

    choice = Prompt.ask("Numbers to download (e.g., 1,3-5, all)", default="all")
    selected = tracks if choice.lower() == "all" else _parse_choice(choice, tracks)
    for track in selected:
        _download_track(track, quality, service, downloader, writer)


def _handle_artist_search(service, query, quality, downloader, writer):
    with console.status("[bold green]Searching for artists..."):
        artists = service.search_artists(query)

    exact_name = normalize_text(query)
    artists = [artist for artist in artists if normalize_text(artist.name) == exact_name] or artists

    if not artists:
        console.print(f"[red]No artists found with the name '{query}'.[/red]")
        return

    metadata_artists = artists and _is_metadata_provider(artists[0].provider)
    columns = ["#", "Artist", "Area", "Type", "About", "MBID"] if metadata_artists else ["#", "Artist", "Provider", "Seed Tracks", "ID"]
    table = _create_compact_table("Found Artists", columns)
    for index, artist in enumerate(artists, 1):
        if metadata_artists:
            table.add_row(
                str(index),
                _display_text(artist.name),
                artist.area or "-",
                artist.type or "-",
                artist.disambiguation or "-",
                str(artist.id),
            )
        else:
            table.add_row(str(index), _display_text(artist.name), _provider_label(artist.provider), str(len(artist.tracks)), str(artist.id))
    console.print(table)

    choice = Prompt.ask("Artist number", default="1")
    try:
        artist = artists[int(choice) - 1]
    except (ValueError, IndexError):
        console.print("[red]Invalid number[/red]")
        return

    with console.status("[bold green]Fetching artist catalog..."):
        strict_tracks = service.get_artist_tracks(artist)

    if not strict_tracks:
        console.print("[red]No tracks found for this artist.[/red]")
        return

    strict_tracks = _sort_artist_tracks(strict_tracks)
    _display_track_table(strict_tracks, "Artist Tracks")

    choice = Prompt.ask("Numbers to download (e.g., 1,3-5, all)", default="all")
    selected = strict_tracks if choice.lower() == "all" else _parse_choice(choice, strict_tracks)
    for track in selected:
        _download_track(track, quality, service, downloader, writer)


def _display_track_table(tracks: list, title: str) -> None:
    """Unified track table display that adapts columns to data source."""
    if not tracks:
        return

    is_metadata = _is_metadata_provider(tracks[0].provider)
    if is_metadata:
        columns = ["#", "Title", "Artist(s)", "Album", "Disc", "Track", "Duration"]
    else:
        columns = ["#", "Title", "Artist(s)", "Album", "Provider", "Quality", "Duration"]

    table = _create_compact_table(title, columns)
    for index, track in enumerate(tracks, 1):
        if is_metadata:
            disc, track_no = _format_disc_track(track)
            table.add_row(
                str(index),
                format_title(track),
                format_artists(track),
                _album_label(track),
                disc,
                track_no,
                _format_duration(track.duration),
            )
        else:
            table.add_row(
                str(index),
                format_title(track),
                format_artists(track),
                _album_label(track),
                _provider_label(track.provider),
                _quality_label(track),
                _format_duration(track.duration),
            )
    console.print(table)


def _sort_artist_tracks(tracks: List) -> List:
    final_tracks = deduplicate_tracks(tracks)
    return sorted(
        final_tracks,
        key=lambda track: (
            _format_release_date(track.releaseDate),
            normalize_text(track.album.title if track.album else ""),
            track.volumeNumber or 0,
            track.trackNumber or 0,
        ),
    )


def _handle_playlist_download(service, url, quality, base_output, writer):
    with console.status("[bold green]Extracting playlist metadata..."):
        extractor = PlaylistExtractor()
        try:
            playlist_name, tracks = extractor.get_tracks(url)
        except Exception as exc:
            console.print(f"[red]Error extracting playlist: {exc}[/red]")
            return

    output_dir = os.path.join(base_output, sanitize_foldername(playlist_name))
    downloader = Downloader(output_dir)
    console.print(f"[bold green]Playlist:[/bold green] {playlist_name}")

    for item in tracks:
        query = f"{item['title']} {item['artist']}".strip()
        console.print(f"\n[cyan]Searching:[/cyan] {query}")
        results = service.search_tracks(query, title_only_fallback=item["title"])
        if not results:
            console.print(f"[red]Not found in any API: {item['title']}[/red]")
            continue
        target_track = results[0]
        _download_track(target_track, quality, service, downloader, writer)


def sanitize_foldername(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()


def _parse_choice(choice, items):
    selected = []
    for part in choice.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_str, end_str = part.split("-", 1)
            try:
                start = int(start_str) - 1
                end = int(end_str) - 1
            except ValueError:
                continue
            for index in range(start, end + 1):
                if 0 <= index < len(items):
                    selected.append(items[index])
            continue
        try:
            index = int(part) - 1
        except ValueError:
            continue
        if 0 <= index < len(items):
            selected.append(items[index])
    return selected


def _download_track(track_ref, quality, service, downloader, writer):
    console.print(f"\n[yellow]Fetching information for track {track_ref.id}...[/yellow]")
    try:
        info, manifest, provider_name = service.resolve_download(track_ref, quality)
        console.print(f"[green]Resolved provider: {_provider_label(provider_name)}[/green]")
        console.print(f"[green]Resolved quality: {_quality_label(info)}[/green]")
        console.print(f"[green]Downloading audio: {info.title}[/green]")
        filepath = downloader.download_track(info.id, manifest, info)

        cover_data = None
        if info.album and info.album.cover:
            console.print("[dim]Downloading cover art...[/dim]")
            cover_data = writer.download_cover(info.album.cover)
            if cover_data:
                console.print("[dim]Cover art successfully downloaded.[/dim]")
            else:
                console.print("[red]Could not download cover art.[/red]")

        console.print("[yellow]Writing metadata to FLAC file...[/yellow]")
        writer.write_flac(filepath, info, cover_data)
        console.print("[bold green]Track successfully processed and saved![/bold green]")
    except DownloadInterrupted as exc:
        console.print(f"[yellow]Download interrupted. Partial file kept at: {exc.path}[/yellow]")
    except Exception as exc:
        console.print(f"[bold red]Error processing track {track_ref.id}: {exc}[/bold red]")


def _create_compact_table(title: str, columns: list) -> Table:
    table = Table(
        title=title,
        box=box.ROUNDED,
        header_style="bold white",
        title_style="bold italic #FFB347",
        border_style="grey50",
        show_edge=True,
        pad_edge=False,
        collapse_padding=True,
    )

    for col in columns:
        if col == "#":
            table.add_column(col, style="#aec7cf", width=4, no_wrap=True)
        elif col == "ID":
            table.add_column(col, style="#aec7cf", width=10, no_wrap=True)
        elif col == "MBID":
            table.add_column(col, style="#aec7cf", width=10, no_wrap=True)
        elif col == "Duration":
            table.add_column(col, style="#ffcc8e", justify="right", width=8, no_wrap=True)
        elif col == "Quality":
            table.add_column(col, style="#b5e48c", width=22, no_wrap=True)
        elif col == "Provider":
            table.add_column(col, style="#cdb4db", width=10, no_wrap=True)
        elif col in {"Disc", "Track"}:
            table.add_column(col, style="#aec7cf", width=5, no_wrap=True)
        else:
            table.add_column(col, overflow="fold")
    return table

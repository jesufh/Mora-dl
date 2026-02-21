import os
import re
import urllib.parse
import unicodedata
import click
import requests
from rich.console import Console
from rich.table import Table
from rich.prompt import Prompt
from rich import box
from .api import APIClient
from .downloader import Downloader
from .metadata import MetadataWriter

console = Console()

@click.group(help="Mora Scraper - Download FLAC music from the hifi API.")
def cli():
    pass

@cli.command(help="Download tracks, albums, or artists based on a search query.")
@click.option("--track", is_flag=True, help="Search for specific tracks")
@click.option("--album", is_flag=True, help="Search for albums")
@click.option("--artist", is_flag=True, help="Search for artists")
@click.option("--query", "-q", required=True, help="Search term/query")
@click.option("--quality", type=click.Choice(["LOSSLESS", "HI_RES_LOSSLESS"]), default="LOSSLESS", help="Audio quality (16/24 bits)")
@click.option("--output", "-o", default="./downloads", help="Output directory path")
def download(track, album, artist, query, quality, output):
    """Main command to handle the downloading process."""
    flags = [track, album, artist]
    if sum(flags) != 1:
        console.print("[red]You must specify exactly one of: --track, --album, --artist[/red]")
        return

    client = APIClient()
    downloader = Downloader(output)
    writer = MetadataWriter()

    if track: _handle_track_search(client, query, quality, downloader, writer)
    elif album: _handle_album_search(client, query, quality, downloader, writer)
    elif artist: _handle_artist_search(client, query, quality, downloader, writer)

def format_artists(artists_data) -> str:
    """Format artist list avoiding duplicate appearances."""
    seen = set()
    artists_list = []
    for a in (artists_data or []):
        name = a.name if hasattr(a, 'name') else a.get('name', '')
        if name:
            name_upper = name.upper()
            if name_upper not in seen:
                seen.add(name_upper)
                artists_list.append(name)
    return ", ".join(artists_list[:3]) + ("..." if len(artists_list) > 3 else "")

def format_title(item) -> str:
    """Combine base title with track version if applicable."""
    title = item.title if hasattr(item, 'title') else item.get('title', '')
    version = item.version if hasattr(item, 'version') else item.get('version', '')
    if version and version.lower() not in title.lower():
        title = f"{title} ({version})"
    return title

def deduplicate_tracks(tracks_list):
    """Filter tracks prioritizing the highest available audio quality."""
    unique_tracks = {}
    qualities = {"LOW": 1, "HIGH": 2, "LOSSLESS": 3, "HI_RES_LOSSLESS": 4}

    for t in tracks_list:
        title_key = normalize_str(t.title)
        album_key = normalize_str(t.album.title if t.album else "")
        key = (title_key, album_key, getattr(t, 'explicit', False))
        
        if key not in unique_tracks:
            unique_tracks[key] = t
        else:
            curr_q = qualities.get(unique_tracks[key].audioQuality, 0)
            new_q = qualities.get(t.audioQuality, 0)
            if new_q > curr_q:
                unique_tracks[key] = t
                
    return list(unique_tracks.values())

def _handle_track_search(client, query, quality, downloader, writer):
    """Search and download specific tracks."""
    with console.status("[bold green]Searching for tracks..."):
        all_tracks = client.search_tracks(query)

    q_lower = query.lower().strip()
    matched_tracks = [t for t in all_tracks if q_lower in t.title.lower()]
    tracks = deduplicate_tracks(matched_tracks)

    if not tracks:
        console.print("[red]No tracks found matching your search exactly.[/red]")
        return

    table = _create_compact_table(":musical_note: Found Tracks", ["#", "Title", "Artist(s)", "Album", "Quality", "Duration"])
    for i, t in enumerate(tracks, 1):
        artists_str = format_artists(t.artists)
        mins, secs = divmod(t.duration, 60)
        table.add_row(
            str(i), 
            format_title(t), 
            artists_str, 
            t.album.title if t.album else "Unknown", 
            t.audioQuality or "N/A", 
            f"{mins}:{secs:02d}"
        )
    console.print(table)

    choice = Prompt.ask("Numbers to download (e.g., 1,3-5, all)", default="all")
    selected = tracks if choice.lower() == "all" else _parse_choice(choice, tracks)
    for track in selected:
        _download_track(track.id, quality, client, downloader, writer)

def _handle_album_search(client, query, quality, downloader, writer):
    """Search and process whole albums."""
    with console.status("[bold green]Searching for albums..."):
        all_tracks = client.search_tracks(query)

    q_lower = query.lower().strip()
    albums_dict = {}
    
    for t in all_tracks:
        if t.album and q_lower in t.album.title.lower():
            if t.album.id not in albums_dict:
                t.album.artist = t.artists[0] if t.artists else None
                albums_dict[t.album.id] = t.album

    albums = list(albums_dict.values())
    if not albums:
        console.print("[red]No albums found matching your search exactly.[/red]")
        return

    table = _create_compact_table(":cd: Found Albums", ["#", "Album", "Artist", "ID"])
    for i, al in enumerate(albums, 1):
        artist_name = al.artist.name if al.artist else "Various"
        table.add_row(str(i), al.title, artist_name, str(al.id))
    console.print(table)

    choice = Prompt.ask("Album number to view tracks (e.g., 1)", default="1")
    try:
        idx = int(choice) - 1
        if idx < 0 or idx >= len(albums):
            console.print("[red]Invalid number[/red]")
            return
        album = albums[idx]
    except ValueError:
        console.print("[red]Invalid number[/red]")
        return

    with console.status(f"[bold green]Fetching tracks for {album.title}..."):
        album_info = client.get_album(album.id)

    if not album_info.items:
        console.print("[red]No tracks found in this album.[/red]")
        return

    table = _create_compact_table(f":cd: Tracks in {album.title}", ["#", "Title", "Artist(s)", "Quality", "Duration", "ID"])
    for i, item in enumerate(album_info.items, 1):
        artists_str = format_artists(item.get("artists", []))
        mins, secs = divmod(item.get("duration", 0), 60)
        table.add_row(
            str(i), 
            format_title(item), 
            artists_str, 
            item.get("audioQuality", "N/A"), 
            f"{mins}:{secs:02d}", 
            str(item.get("id", ""))
        )
    console.print(table)

    choice = Prompt.ask("Numbers to download (e.g., 1,3-5, all)", default="all")
    selected = album_info.items if choice.lower() == "all" else _parse_choice(choice, album_info.items, is_dict=True)
    for item in selected:
        track_id = item.get("id")
        if track_id:
            _download_track(track_id, quality, client, downloader, writer)

def normalize_str(s):
    """Normalize string for catalog authentication."""
    if not s: return ""
    s = s.lower().strip()
    s = re.sub(r'\(.*?\)', '', s)  
    s = re.sub(r'\[.*?\]', '', s)
    s = ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')
    s = re.sub(r'[^a-z0-9]', '', s) 
    return s

def get_itunes_fingerprint(artist_name: str):
    """Fetch iTunes catalog fingerprint to verify official releases."""
    search_url = f"https://itunes.apple.com/search?term={urllib.parse.quote(artist_name)}&entity=song&limit=50"
    albums = set()
    tracks = set()
    try:
        resp = requests.get(search_url, timeout=5)
        if resp.status_code != 200:
            return albums, tracks
            
        results = resp.json().get("results", [])
        artist_ids = {}
        target_name = artist_name.lower()
        
        for item in results:
            if item.get("artistName", "").lower() == target_name:
                aid = item.get("artistId")
                if aid:
                    artist_ids[aid] = artist_ids.get(aid, 0) + 1

        if not artist_ids:
            return albums, tracks

        main_artist_id = max(artist_ids, key=artist_ids.get)
        lookup_url = f"https://itunes.apple.com/lookup?id={main_artist_id}&entity=song&limit=200"
        resp2 = requests.get(lookup_url, timeout=5)
        
        if resp2.status_code == 200:
            for item in resp2.json().get("results", []):
                if item.get("collectionName"):
                    albums.add(normalize_str(item["collectionName"]))
                if item.get("trackName"):
                    tracks.add(normalize_str(item["trackName"]))

        for item in results:
            if target_name in item.get("artistName", "").lower():
                if item.get("artistName", "").lower() == target_name:
                    if item.get("artistId") != main_artist_id:
                        continue 
                if item.get("collectionName"): 
                    albums.add(normalize_str(item["collectionName"]))
                if item.get("trackName"): 
                    tracks.add(normalize_str(item["trackName"]))
    except Exception:
        pass
    return albums, tracks

def _handle_artist_search(client, query, quality, downloader, writer):
    """Search and download an artist's official, authenticated discography."""
    with console.status("[bold green]Searching for artists..."):
        tracks = client.search_tracks(query)

    artists_dict = {}
    q_lower = query.lower().strip()
    for t in tracks:
        for a in (t.artists or []):
            if a.name and a.name.lower().strip() == q_lower:
                if a.id not in artists_dict:
                    artists_dict[a.id] = a

    artists = list(artists_dict.values())
    if not artists:
        console.print(f"[red]No artists found with the exact name '{query}'.[/red]")
        return

    artist_table = _create_compact_table(":microphone: Found Artists", ["#", "Artist", "ID"])
    for i, a in enumerate(artists, 1):
        artist_table.add_row(str(i), a.name, str(a.id))
    console.print(artist_table)

    choice = Prompt.ask("Artist number (e.g., 1)", default="1")
    try:
        idx = int(choice) - 1
        if idx < 0 or idx >= len(artists):
            console.print("[red]Invalid number[/red]")
            return
        artist = artists[idx]
    except ValueError:
        console.print("[red]Invalid number[/red]")
        return

    with console.status(f"[bold green]Authenticating discography (Removing false metadata)..."):
        artist_info = client.get_artist(artist.id)

    if not artist_info.tracks:
        console.print("[red]No tracks found for this artist.[/red]")
        return

    strict_tracks = []
    for t in artist_info.tracks:
        track_artists = t.artists or []
        if t.artist: track_artists.append(t.artist)
        for a in track_artists:
            if a and a.name and a.id:
                if str(a.id) == str(artist.id) and a.name.lower().strip() == artist.name.lower().strip():
                    strict_tracks.append(t)
                    break

    unique_tracks_list = deduplicate_tracks(strict_tracks)
    unique_tracks = {t.id: t for t in unique_tracks_list}

    itunes_albums, itunes_tracks = get_itunes_fingerprint(artist.name)
    
    verified_album_ids = set()
    if itunes_albums or itunes_tracks:
        for t in unique_tracks.values():
            n_album = normalize_str(t.album.title if t.album else "")
            n_track = normalize_str(t.title)
            if n_album in itunes_albums or n_track in itunes_tracks:
                if t.album:
                    verified_album_ids.add(t.album.id)

    final_tracks = []
    discarded_albums = set()
    
    for t in unique_tracks.values():
        is_verified = False
        if not itunes_albums and not itunes_tracks:
            is_verified = True if (t.popularity or 0) >= 5 else False
        else:
            n_album = normalize_str(t.album.title if t.album else "")
            n_track = normalize_str(t.title)

            if t.album and t.album.id in verified_album_ids: is_verified = True
            elif n_track in itunes_tracks: is_verified = True
            elif n_album in itunes_albums: is_verified = True
            elif t.popularity and t.popularity >= 50: is_verified = True

        if is_verified:
            final_tracks.append(t)
        else:
            if t.album: discarded_albums.add(t.album.title)

    if not final_tracks:
        final_tracks = list(unique_tracks.values())

    album_pops = {}
    for t in final_tracks:
        al_key = normalize_str(t.album.title if t.album else "")
        pop = t.popularity or 0
        if al_key not in album_pops or pop > album_pops[al_key]:
            album_pops[al_key] = pop

    sorted_tracks = sorted(final_tracks, key=lambda x: (
        -album_pops.get(normalize_str(x.album.title if x.album else ""), 0),
        normalize_str(x.album.title if x.album else ""),
        x.trackNumber or 0
    ))

    if discarded_albums:
        console.print(f"\n[bold red]:heavy_check_mark: Catalog purified![/bold red] [dim]Hid {len(discarded_albums)} impostor releases.[/dim]\n")

    track_table = _create_compact_table(
        f":musical_note: Official Tracks for {artist.name}",
        ["#", "Title", "Artist(s)", "Album", "Quality", "Duration"]
    )
    
    for i, t in enumerate(sorted_tracks, 1):
        artists_str = format_artists(t.artists)
        mins, secs = divmod(t.duration, 60)
        track_table.add_row(
            str(i), 
            format_title(t), 
            artists_str, 
            t.album.title if t.album else "Unknown", 
            t.audioQuality or "N/A", 
            f"{mins}:{secs:02d}"
        )
    console.print(track_table)

    choice = Prompt.ask("Numbers to download (e.g., 1,3-5, all)", default="all")
    selected = sorted_tracks if choice.lower() == "all" else _parse_choice(choice, sorted_tracks)
    for track in selected:
        _download_track(track.id, quality, client, downloader, writer)

def _parse_choice(choice, items, is_dict=False):
    """Parse user selection string into a list of items."""
    selected = []
    parts = choice.split(",")
    for part in parts:
        part = part.strip()
        if "-" in part:
            start, end = map(int, part.split("-"))
            for idx in range(start - 1, end):
                if 0 <= idx < len(items):
                    selected.append(items[idx])
        else:
            idx = int(part) - 1
            if 0 <= idx < len(items):
                selected.append(items[idx])
    return selected

def _download_track(track_id, quality, client, downloader, writer):
    """Process individual track download and apply ID3 tags."""
    console.print(f"\n[yellow]Fetching information for track {track_id}...[/yellow]")
    try:
        info = client.get_track_info(track_id)
        try:
            manifest = client.get_track_manifest(track_id, quality)
        except Exception:
            alt_quality = "HI_RES_LOSSLESS" if quality == "LOSSLESS" else "LOSSLESS"
            console.print(f"[yellow]Retrying with quality {alt_quality}...[/yellow]")
            manifest = client.get_track_manifest(track_id, alt_quality)

        console.print(f"[green]Downloading audio: {info.title}[/green]")
        filepath = downloader.download_track(track_id, manifest, info)

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

    except Exception as e:
        console.print(f"[bold red]Error processing track {track_id}: {e}[/bold red]")

def _create_compact_table(title: str, columns: list) -> Table:
    """Create a strictly formatted, single-line table with smart truncation."""
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
        elif col == "Duration":
            table.add_column(col, style="#ffcc8e", justify="right", width=8, no_wrap=True) 
        elif col == "Quality":
            table.add_column(col, style="#ffcc8e", justify="center", width=15, no_wrap=True)
        elif col == "Title":
            table.add_column(col, max_width=35, no_wrap=True, overflow="ellipsis")
        elif col == "Artist(s)" or col == "Artist":
            table.add_column(col, max_width=25, no_wrap=True, overflow="ellipsis")
        elif col == "Album":
            table.add_column(col, max_width=25, no_wrap=True, overflow="ellipsis")
        else:
            table.add_column(col, no_wrap=True, overflow="ellipsis") 

    return table

if __name__ == "__main__":
    cli()
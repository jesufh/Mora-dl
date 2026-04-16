import os

import click
from rich import box
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table
from tqdm import tqdm

from .audio import TidalProvider
from .backup import decryption_contexts, primary_backup_context, search_tokens
from .config import load_passphrase, setup_config
from .crypto import MoraCrypto
from .downloader import AudioDownloader
from .metadata import DeezerProvider
from .models import Track
from .playlist import SpotifyExtractor
from .telegram_cloud import TelegramCloud

console = Console()


def parse_selection(choice: str, max_val: int) -> list[int]:
    if choice.lower().strip() == "all":
        return list(range(max_val))

    selected = set()
    for part in choice.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                start, end = map(int, part.split("-"))
                selected.update(range(start - 1, end))
            except ValueError:
                pass
        else:
            try:
                selected.add(int(part) - 1)
            except ValueError:
                pass

    return [index for index in sorted(selected) if 0 <= index < max_val]


def format_duration(seconds: int) -> str:
    mins, secs = divmod(seconds, 60)
    return f"{mins}:{secs:02d}"


def process_downloads(
    tracks: list[Track],
    quality: str,
    output: str,
    deezer: DeezerProvider,
    tidal: TidalProvider,
    tg_cloud: TelegramCloud | None = None,
    crypto: MoraCrypto | None = None,
):
    downloader = AudioDownloader(output_dir=output)
    backup_enabled = tg_cloud is not None and crypto is not None

    for idx, track in enumerate(tracks, 1):
        console.print(f"\n[bold cyan]({idx}/{len(tracks)}) Processing:[/bold cyan] {track.display_name}")
        try:
            with console.status("[dim]Completing metadata (Deezer)..."):
                track = deezer.get_track_details(track)

            filename = downloader._sanitize(f"{track.artist} - {track.title}.flac")
            output_path = os.path.join(output, filename)
            enc_path = output_path + ".enc"
            found_in_tg = False

            if backup_enabled:
                backup_context = primary_backup_context(track)
                candidate_tokens = search_tokens(track, crypto)

                with console.status("[dim]Searching backup in Telegram..."):
                    msg = tg_cloud.search_track(candidate_tokens)

                if msg:
                    console.print("[green]Backup found in the cloud. Starting Zero-Trust recovery...[/green]")
                    with tqdm(total=msg.document.size, unit="B", unit_scale=True, desc="Downloading") as bar:
                        tg_cloud.download_track(msg, enc_path, progress_callback=lambda c, t: bar.update(c - bar.n))

                    console.print("[dim]Decrypting and verifying integrity...[/dim]")
                    for context in decryption_contexts(track):
                        if crypto.decrypt_file(enc_path, output_path, context):
                            console.print("[bold green]✓ File recovered successfully from the Data Lake.[/bold green]")
                            found_in_tg = True
                            break

                    if not found_in_tg:
                        console.print("[red]✗ Integrity error (corrupt file). Falling back to audio APIs...[/red]")
                        if os.path.exists(output_path):
                            os.remove(output_path)

                    if os.path.exists(enc_path):
                        os.remove(enc_path)

            if not found_in_tg:
                with console.status("[dim]Searching in HQ audio API (Tidal)..."):
                    audio_id = tidal.find_track_id(track.title, track.artist, track.duration)
                    if not audio_id:
                        console.print("[yellow]Track not found in the audio APIs. Skipping.[/yellow]")
                        continue
                    manifest = tidal.get_stream_manifest(audio_id, quality)

                console.print(
                    f"[green]API source found:[/green] {manifest['quality']} | "
                    f"{manifest['bitDepth']}bit/{manifest['sampleRate']}Hz"
                )
                downloader.download_and_tag(track, manifest)

                if backup_enabled:
                    console.print("[dim]Encrypting Zero-Trust backup for the Data Lake...[/dim]")
                    backup_context = primary_backup_context(track)
                    storage_token = crypto.storage_token(backup_context)
                    crypto.encrypt_file(output_path, enc_path, backup_context)

                    file_size = os.path.getsize(enc_path)
                    with tqdm(total=file_size, unit="B", unit_scale=True, desc="Backing up") as bar:
                        tg_cloud.upload_track(enc_path, storage_token, progress_callback=lambda c, t: bar.update(c - bar.n))

                    if os.path.exists(enc_path):
                        os.remove(enc_path)
                    console.print("[bold green]✓ Saved to disk and backed up to Telegram Cloud.[/bold green]")
                else:
                    console.print("[bold green]✓ Saved locally.[/bold green]")

        except Exception as e:
            console.print(f"[bold red]✗ Error processing track:[/bold red] {e}")


@click.command()
@click.option("--track", is_flag=True, help="Search track")
@click.option("--album", is_flag=True, help="Search album")
@click.option("--artist", is_flag=True, help="Search artist")
@click.option("--playlist", is_flag=True, help="Download from Spotify URL")
@click.option("--query", "-q", required=True, help="Search term or URL")
@click.option("--quality", "-Q", default="HI_RES_LOSSLESS", type=click.Choice(["LOSSLESS", "HI_RES_LOSSLESS"]))
@click.option("--output", "-o", default="./downloads", help="Output directory")
@click.option("--backup", "backup_override", flag_value=True, default=None, help="Enable and remember Telegram backup.")
@click.option("--no-backup", "backup_override", flag_value=False, help="Disable and remember Telegram backup.")
def cli(track, album, artist, playlist, query, quality, output, backup_override):
    flags = sum([track, album, artist, playlist])
    if flags == 0:
        track = True
    elif flags > 1:
        console.print("[red]Use only one search mode at a time.[/red]")
        return

    config, crypto_created = setup_config(console, backup_override=backup_override)
    backup_enabled = config.get("backup_enabled", True)
    crypto = None
    tg_cloud = None

    if backup_enabled:
        passphrase = load_passphrase(console, confirm=crypto_created)
        crypto = MoraCrypto(passphrase, config["crypto"])
        tg_cloud = TelegramCloud(
            config["api_id"],
            config["api_hash"],
            config["telegram_target"],
            session_name=config.get("telegram_session", "mora_backup"),
        )

    try:
        if tg_cloud:
            try:
                tg_cloud.start()
            except Exception as e:
                console.print(f"[bold red]✗ Error connecting to Telegram:[/bold red] {e}")
                return

        deezer = DeezerProvider()
        tidal = TidalProvider()

        if playlist:
            extractor = SpotifyExtractor()
            with console.status("[bold green]Extracting Spotify playlist..."):
                try:
                    pl_name, pl_tracks = extractor.extract_tracks(query)
                except Exception as e:
                    console.print(f"[red]Playlist extraction error:[/red] {e}")
                    return

            console.print(f"[bold magenta]Playlist:[/bold magenta] {pl_name} ({len(pl_tracks)} tracks)")
            resolved_tracks = []
            with console.status("[bold green]Matching songs with Deezer for exact metadata..."):
                for item in pl_tracks:
                    results = deezer.search_tracks(f"{item['title']} {item['artist']}", limit=1)
                    if results:
                        resolved_tracks.append(results[0])

            if not resolved_tracks:
                return
            process_downloads(
                resolved_tracks,
                quality,
                os.path.join(output, pl_name.replace("/", "-")),
                deezer,
                tidal,
                tg_cloud,
                crypto,
            )
            return

        if artist:
            with console.status("[bold green]Searching artists..."):
                artists = deezer.search_artists(query)
            if not artists:
                return

            table = Table(box=box.ROUNDED, header_style="bold cyan")
            table.add_column("#", justify="right", style="dim")
            table.add_column("Artist", style="bold white")
            table.add_column("Fans", justify="right", style="#f2ccbf")
            for i, artist_item in enumerate(artists, 1):
                table.add_row(str(i), artist_item.name, f"{artist_item.fan_count:,}")
            console.print(table)

            choice = Prompt.ask("\n[bold yellow]Choose the artist (or 'q' to exit)[/bold yellow]")
            if choice.lower() == "q":
                return
            selected_art = artists[int(choice) - 1]

            with console.status("[bold green]Loading top tracks..."):
                tracks = deezer.get_artist_top_tracks(selected_art)
            target_output = os.path.join(output, selected_art.name.replace("/", "-"))

        elif album:
            with console.status("[bold green]Searching albums..."):
                albums = deezer.search_albums(query)
            if not albums:
                return

            table = Table(box=box.ROUNDED, header_style="bold cyan")
            table.add_column("#", justify="right", style="dim")
            table.add_column("Album", style="bold white")
            table.add_column("Artist", style="#f2ccbf")
            table.add_column("Tracks", justify="right", style="dim")
            for i, album_item in enumerate(albums, 1):
                table.add_row(str(i), album_item.title, album_item.artist, str(album_item.track_count))
            console.print(table)

            choice = Prompt.ask("\n[bold yellow]Choose the album (or 'q' to exit)[/bold yellow]")
            if choice.lower() == "q":
                return
            selected_alb = albums[int(choice) - 1]

            with console.status("[bold green]Loading tracks..."):
                tracks = deezer.get_album_tracks(selected_alb)
            target_output = os.path.join(output, f"{selected_alb.artist} - {selected_alb.title}".replace("/", "-"))

        else:
            with console.status("[bold green]Searching tracks..."):
                tracks = deezer.search_tracks(query)
            if not tracks:
                return
            target_output = output

        track_table = Table(box=box.ROUNDED, header_style="#bfd3f2")
        track_table.add_column("#", justify="center", style="dim")
        track_table.add_column("Title", style="bold white")
        track_table.add_column("Artist", style="dim")
        track_table.add_column("Album", style="dim")
        track_table.add_column("Duration", justify="center", style="#f2ccbf")

        for i, track_item in enumerate(tracks, 1):
            track_table.add_row(
                str(i),
                track_item.title,
                track_item.artist,
                track_item.album,
                format_duration(track_item.duration),
            )

        console.print(track_table)

        choice = Prompt.ask(
            "\n[bold yellow]Select songs (for example: 1, 3-5, all) or 'q' to exit[/bold yellow]",
            default="all" if (album or playlist) else "1",
        )
        if choice.lower() == "q":
            return

        indices = parse_selection(choice, len(tracks))
        if not indices:
            return

        process_downloads([tracks[i] for i in indices], quality, target_output, deezer, tidal, tg_cloud, crypto)

    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")
    finally:
        if tg_cloud:
            tg_cloud.disconnect()


if __name__ == "__main__":
    cli()

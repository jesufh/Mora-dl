import base64
import getpass
import json
import os
import shutil
from pathlib import Path

CONFIG_FILE = "mora_config.json"
DEFAULT_CHUNK_SIZE = 1024 * 1024
DEFAULT_ARGON2_MEMORY_COST = 64 * 1024
DEFAULT_TELEGRAM_SESSION = "mora_backup"


def load_config() -> dict:
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, "r", encoding="utf-8") as handle:
            return json.load(handle)
    return {}


def save_config(config: dict):
    with open(CONFIG_FILE, "w", encoding="utf-8") as handle:
        json.dump(config, handle, indent=4)


def _prompt_bool(console, prompt: str, default: bool) -> bool:
    suffix = "[Y/n]" if default else "[y/N]"
    answer = console.input(f"{prompt} {suffix}: ").strip().lower()
    if not answer:
        return default
    return answer in {"y", "yes", "1", "true"}


def _default_crypto_config() -> dict:
    return {
        "index_key": base64.b64encode(os.urandom(32)).decode("ascii"),
        "salt": base64.b64encode(os.urandom(16)).decode("ascii"),
        "chunk_size": DEFAULT_CHUNK_SIZE,
        "argon2": {
            "iterations": 3,
            "lanes": max(1, min(4, os.cpu_count() or 1)),
            "memory_cost": DEFAULT_ARGON2_MEMORY_COST,
        },
    }


def _ensure_crypto_config(config: dict) -> tuple[dict, bool]:
    crypto = config.get("crypto")
    created = not crypto
    if created:
        crypto = _default_crypto_config()
    else:
        crypto = dict(crypto)
        crypto.setdefault("index_key", base64.b64encode(os.urandom(32)).decode("ascii"))
        crypto.setdefault("salt", base64.b64encode(os.urandom(16)).decode("ascii"))
        crypto.setdefault("chunk_size", DEFAULT_CHUNK_SIZE)
        argon2 = dict(crypto.get("argon2") or {})
        argon2.setdefault("iterations", 3)
        argon2.setdefault("lanes", max(1, min(4, os.cpu_count() or 1)))
        argon2.setdefault("memory_cost", DEFAULT_ARGON2_MEMORY_COST)
        crypto["argon2"] = argon2
    config["crypto"] = crypto
    config.pop("aes_key", None)
    return config, created


def _session_sidecars(session_file: Path) -> list[Path]:
    return [
        session_file.with_name(session_file.name + suffix)
        for suffix in ("-journal", "-wal", "-shm")
    ]


def _migrate_legacy_session(session_name: str) -> None:
    session_file = Path(session_name)
    if session_file.suffix != ".session":
        session_file = session_file.with_suffix(".session")

    legacy_file = Path("mora_session.session")
    if session_file.exists() or not legacy_file.exists():
        return

    shutil.copy2(legacy_file, session_file)
    for legacy_sidecar, target_sidecar in zip(
        _session_sidecars(legacy_file),
        _session_sidecars(session_file),
    ):
        if legacy_sidecar.exists():
            shutil.copy2(legacy_sidecar, target_sidecar)


def _ensure_backup_enabled(config: dict, console, backup_override: bool | None) -> bool:
    if backup_override is not None:
        enabled = backup_override
    elif "backup_enabled" in config:
        enabled = bool(config["backup_enabled"])
    elif "cloud_enabled" in config:
        enabled = bool(config.pop("cloud_enabled"))
    elif any(config.get(key) for key in ("api_id", "api_hash", "telegram_target", "telegram_session")) or config.get("crypto"):
        enabled = True
    else:
        console.print("\n[bold yellow]Telegram backup is now optional.[/bold yellow]")
        enabled = _prompt_bool(console, "Enable Telegram backup", True)

    config["backup_enabled"] = enabled
    return enabled


def setup_config(console, backup_override: bool | None = None) -> tuple[dict, bool]:
    config = load_config()
    backup_enabled = _ensure_backup_enabled(config, console, backup_override)
    crypto_created = False

    if backup_enabled:
        if "api_id" not in config or "api_hash" not in config:
            console.print("\n[bold yellow]Initial Telegram setup required.[/bold yellow]")
            console.print("Get your free credentials at: [cyan]https://my.telegram.org/apps[/cyan]")
            config["api_id"] = int(console.input("API ID: "))
            config["api_hash"] = console.input("API HASH: ")

        if not config.get("telegram_target"):
            console.print("\n[bold yellow]Set the channel or group Mora will use for backup.[/bold yellow]")
            console.print("Accepts @username, a public link, a private link already resolved by your session, or a numeric ID.")
            config["telegram_target"] = console.input("Backup channel or group: ").strip()

        if not config.get("telegram_session"):
            config["telegram_session"] = DEFAULT_TELEGRAM_SESSION
            _migrate_legacy_session(config["telegram_session"])

        config, crypto_created = _ensure_crypto_config(config)

    save_config(config)
    return config, crypto_created


def load_passphrase(console, confirm: bool = False) -> str:
    env_passphrase = os.getenv("MORA_PASSPHRASE")
    if env_passphrase:
        return env_passphrase

    console.print("\n[bold yellow]Encryption passphrase required.[/bold yellow]")
    console.print("It is used to derive the master key and recover your backups.")

    first = getpass.getpass("Encryption passphrase: ")
    if not first:
        raise ValueError("The passphrase cannot be empty.")
    if not confirm:
        return first

    second = getpass.getpass("Confirm the passphrase: ")
    if first != second:
        raise ValueError("The passphrases do not match.")
    return first

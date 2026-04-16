import asyncio
import logging
import os
import time
from pathlib import Path

from telethon.sync import TelegramClient
from telethon.tl.types import DocumentAttributeFilename


def _telethon_logger() -> logging.Logger:
    logger = logging.getLogger("mora.telethon")
    if getattr(logger, "_mora_configured", False):
        return logger

    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.FileHandler("mora_telegram.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger._mora_configured = True
    return logger


def _configure_asyncio_policy() -> None:
    if os.name != "nt":
        return
    if isinstance(asyncio.get_event_loop_policy(), asyncio.WindowsSelectorEventLoopPolicy):
        return
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class SessionLock:
    def __init__(self, session_name: str):
        session_file = Path(session_name)
        if session_file.suffix != ".session":
            session_file = session_file.with_suffix(".session")
        self.path = session_file.with_name(session_file.name + ".lock")
        self._fd = None
        self._held = False

    @staticmethod
    def _pid_exists(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def acquire(self):
        while True:
            try:
                self._fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    payload = self.path.read_text(encoding="utf-8").strip().split(",", 1)
                    pid = int(payload[0])
                except (FileNotFoundError, ValueError):
                    pid = 0

                if pid and self._pid_exists(pid):
                    raise RuntimeError(
                        "The Telegram session is already in use by another Mora process. "
                        "Close the other instance or remove the lock if it was left behind."
                    )

                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue

            payload = f"{os.getpid()},{int(time.time())}"
            os.write(self._fd, payload.encode("utf-8"))
            self._held = True
            return

    def release(self):
        if not self._held:
            return
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        self._held = False


class TelegramCloud:
    def __init__(self, api_id: int, api_hash: str, target: str, session_name: str = "mora_backup", client=None):
        self._owns_client = client is None
        self._session_lock = SessionLock(session_name) if self._owns_client else None
        if self._owns_client:
            _configure_asyncio_policy()
        self.client = client or TelegramClient(
            session_name,
            api_id,
            api_hash,
            request_retries=5,
            connection_retries=5,
            retry_delay=1,
            auto_reconnect=True,
            receive_updates=False,
            catch_up=False,
            base_logger=_telethon_logger(),
        )
        if hasattr(getattr(self.client, "session", None), "save_entities"):
            self.client.session.save_entities = False

        self.target_ref = target
        self.target = None
        self.part_size_kb = 512

    def start(self):
        if self._session_lock:
            self._session_lock.acquire()
        try:
            self.client.start()
            self.target = self.client.get_entity(self._resolve_target())
        except Exception:
            if self._session_lock:
                self._session_lock.release()
            raise

    def disconnect(self):
        disconnect = getattr(self.client, "disconnect", None)
        try:
            if callable(disconnect):
                disconnect()
        finally:
            if self._session_lock:
                self._session_lock.release()

    def _resolve_target(self):
        target = self.target_ref.strip()
        if target.lstrip("-").isdigit():
            return int(target)
        return target

    @staticmethod
    def _storage_marker(storage_token: str) -> str:
        return f"mora_v2_{storage_token}"

    @staticmethod
    def _document_name(message) -> str | None:
        if not getattr(message, "document", None):
            return None
        for attribute in getattr(message.document, "attributes", []):
            file_name = getattr(attribute, "file_name", None)
            if file_name:
                return file_name
        return None

    def _message_marker(self, message) -> str | None:
        caption = (getattr(message, "message", None) or "").strip()
        if caption.startswith("mora_v2_"):
            return caption

        file_name = self._document_name(message)
        if file_name and file_name.startswith("mora_v2_"):
            return file_name.removesuffix(".mora")
        return None

    def _message_matches_tokens(self, message, storage_tokens: list[str]) -> bool:
        marker = self._message_marker(message)
        if not marker:
            return False
        expected_markers = {self._storage_marker(token) for token in storage_tokens}
        return marker in expected_markers

    def search_track(self, storage_tokens: list[str] | str):
        if isinstance(storage_tokens, str):
            storage_tokens = [storage_tokens]

        for storage_token in storage_tokens:
            query = self._storage_marker(storage_token)
            for msg in self.client.iter_messages(self.target, search=query, limit=10):
                if msg.document and self._message_matches_tokens(msg, [storage_token]):
                    return msg
        return None

    def download_track(self, message, dest_path: str, progress_callback=None):
        self.client.download_media(
            message,
            file=dest_path,
            progress_callback=progress_callback,
        )

    def upload_track(self, file_path: str, storage_token: str, progress_callback=None):
        marker = self._storage_marker(storage_token)
        self.client.send_file(
            self.target,
            file_path,
            caption=marker,
            force_document=True,
            file_name=f"{marker}.mora",
            part_size_kb=self.part_size_kb,
            attributes=[DocumentAttributeFilename(f"{marker}.mora")],
            progress_callback=progress_callback,
        )

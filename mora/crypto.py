import base64
import hashlib
import hmac
import json
import os
import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

MAGIC = b"MORAENC2"
HEADER_SIZE_BYTES = 4
WRAPPED_FILE_KEY_SIZE = 48
AUTH_TAG_SIZE = 16
NONCE_PREFIX_SIZE = 8


class MoraCrypto:
    def __init__(self, passphrase: str, crypto_config: dict):
        self.chunk_size = int(crypto_config["chunk_size"])
        self.salt = base64.b64decode(crypto_config["salt"])
        self.argon2 = dict(crypto_config["argon2"])
        self.master_key = self._derive_master_key(passphrase.encode("utf-8"))
        self.wrap_key = self._expand_key(self.master_key, b"mora-wrap-key-v2")
        self.legacy_index_key = self._expand_key(self.master_key, b"mora-index-key-v2")
        configured_index_key = crypto_config.get("index_key")
        if configured_index_key:
            self.index_key = base64.b64decode(configured_index_key)
        else:
            self.index_key = self.legacy_index_key

    def storage_token(self, context: str) -> str:
        digest = hmac.new(self.index_key, context.encode("utf-8"), hashlib.sha256).hexdigest()
        return digest[:32]

    def legacy_storage_token(self, track_id: str) -> str:
        digest = hmac.new(self.legacy_index_key, track_id.encode("utf-8"), hashlib.sha256).hexdigest()
        return digest[:32]

    def encrypt_file(self, file_path: str, out_path: str, context: str):
        file_size = os.path.getsize(file_path)
        chunk_count = self._chunk_count(file_size, self.chunk_size)
        if chunk_count > 0xFFFFFFFF:
            raise ValueError("The file exceeds the maximum supported block count.")

        wrap_nonce = os.urandom(12)
        file_nonce_prefix = os.urandom(NONCE_PREFIX_SIZE)
        file_key = AESGCM.generate_key(bit_length=256)
        header = {
            "algorithm": "AES-256-GCM",
            "chunk_size": self.chunk_size,
            "file_size": file_size,
            "nonce_prefix": base64.b64encode(file_nonce_prefix).decode("ascii"),
            "version": 2,
            "wrap_nonce": base64.b64encode(wrap_nonce).decode("ascii"),
        }
        header_blob = self._header_blob(header)
        wrapped_file_key = AESGCM(self.wrap_key).encrypt(
            wrap_nonce,
            file_key,
            self._wrap_aad(context, header_blob),
        )
        file_cipher = AESGCM(file_key)
        temp_path = out_path + ".tmp"

        try:
            with open(file_path, "rb") as source, open(temp_path, "wb") as target:
                target.write(header_blob)
                target.write(wrapped_file_key)
                header_digest = hashlib.sha256(header_blob).digest()

                for chunk_index in range(chunk_count):
                    chunk = source.read(self.chunk_size)
                    nonce = self._chunk_nonce(file_nonce_prefix, chunk_index)
                    aad = self._chunk_aad(context, header_digest, chunk_index)
                    target.write(file_cipher.encrypt(nonce, chunk, aad))

            os.replace(temp_path, out_path)
        except Exception:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise

    def decrypt_file(self, file_path: str, out_path: str, context: str) -> bool:
        temp_path = out_path + ".tmp"
        try:
            with open(file_path, "rb") as source:
                header_blob, header = self._read_header(source)
                wrapped_file_key = source.read(WRAPPED_FILE_KEY_SIZE)
                if len(wrapped_file_key) != WRAPPED_FILE_KEY_SIZE:
                    raise ValueError("Incomplete encryption header.")

                wrap_nonce = base64.b64decode(header["wrap_nonce"])
                file_nonce_prefix = base64.b64decode(header["nonce_prefix"])
                file_key = AESGCM(self.wrap_key).decrypt(
                    wrap_nonce,
                    wrapped_file_key,
                    self._wrap_aad(context, header_blob),
                )
                file_cipher = AESGCM(file_key)
                file_size = int(header["file_size"])
                chunk_size = int(header["chunk_size"])
                chunk_count = self._chunk_count(file_size, chunk_size)
                header_digest = hashlib.sha256(header_blob).digest()

                with open(temp_path, "wb") as target:
                    for chunk_index in range(chunk_count):
                        plain_size = min(chunk_size, file_size - (chunk_index * chunk_size))
                        encrypted_size = plain_size + AUTH_TAG_SIZE
                        chunk = source.read(encrypted_size)
                        if len(chunk) != encrypted_size:
                            raise ValueError("Encrypted file is truncated.")
                        nonce = self._chunk_nonce(file_nonce_prefix, chunk_index)
                        aad = self._chunk_aad(context, header_digest, chunk_index)
                        target.write(file_cipher.decrypt(nonce, chunk, aad))

                    if source.read(1):
                        raise ValueError("Encrypted file contains trailing data.")

            os.replace(temp_path, out_path)
            return True
        except (InvalidTag, ValueError, OSError):
            if os.path.exists(temp_path):
                os.remove(temp_path)
            return False

    def _derive_master_key(self, passphrase: bytes) -> bytes:
        kdf = Argon2id(
            salt=self.salt,
            length=32,
            iterations=int(self.argon2["iterations"]),
            lanes=int(self.argon2["lanes"]),
            memory_cost=int(self.argon2["memory_cost"]),
        )
        return kdf.derive(passphrase)

    @staticmethod
    def _expand_key(key: bytes, info: bytes) -> bytes:
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=None,
            info=info,
        )
        return hkdf.derive(key)

    @staticmethod
    def _chunk_count(file_size: int, chunk_size: int) -> int:
        return 0 if file_size == 0 else ((file_size - 1) // chunk_size) + 1

    @staticmethod
    def _chunk_nonce(nonce_prefix: bytes, chunk_index: int) -> bytes:
        return nonce_prefix + struct.pack(">I", chunk_index)

    @staticmethod
    def _chunk_aad(track_id: str, header_digest: bytes, chunk_index: int) -> bytes:
        return b"mora-chunk-v2" + header_digest + struct.pack(">I", chunk_index) + track_id.encode("utf-8")

    @staticmethod
    def _wrap_aad(track_id: str, header_blob: bytes) -> bytes:
        return b"mora-wrap-v2" + hashlib.sha256(header_blob).digest() + track_id.encode("utf-8")

    @staticmethod
    def _header_blob(header: dict) -> bytes:
        payload = json.dumps(header, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return MAGIC + struct.pack(">I", len(payload)) + payload

    @staticmethod
    def _read_header(source) -> tuple[bytes, dict]:
        magic = source.read(len(MAGIC))
        if magic != MAGIC:
            raise ValueError("Unsupported encryption format.")

        header_size_raw = source.read(HEADER_SIZE_BYTES)
        if len(header_size_raw) != HEADER_SIZE_BYTES:
            raise ValueError("Incomplete header.")
        header_size = struct.unpack(">I", header_size_raw)[0]
        payload = source.read(header_size)
        if len(payload) != header_size:
            raise ValueError("Truncated header.")
        header_blob = magic + header_size_raw + payload
        return header_blob, json.loads(payload.decode("utf-8"))

"""
core/data.py — компонент data (base64, hashes, deflate, random, crypto).

Криптографические методы (AES-CFB, ECDSA, ECDH на P-256/P-384) требуют
опциональной зависимости `cryptography`. Если её нет — методы возвращают
(nil, "not implemented"), как и раньше.
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import logging
import os
import zlib

from core import component as component_mod

logger = logging.getLogger("lupi.data")

DATA_LIMIT = 1024 * 1024  # 1 MiB

# Опциональный backend
try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives.asymmetric import ec as _ec
    from cryptography.hazmat.primitives import hashes as _hashes
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PrivateFormat, PublicFormat, NoEncryption,
        load_pem_private_key, load_pem_public_key,
    )
    _CRYPTO_OK = True
except ImportError:
    _CRYPTO_OK = False
    Cipher = algorithms = modes = None


def _to_lua(b: bytes) -> str:
    return b.decode("latin-1")


def _from_lua(s) -> bytes:
    if isinstance(s, bytes):
        return s
    if not isinstance(s, str):
        s = str(s)
    try:
        return s.encode("latin-1")
    except UnicodeEncodeError:
        return s.encode("utf-8")


def _extract_bytes(x):
    """bytes | latin-1 str → bytes. Иначе None."""
    if isinstance(x, bytes):
        return x
    if isinstance(x, str):
        return _from_lua(x)
    return None


def _extract_key_bytes(key):
    """Принимает lua-таблицу-обёртку ключа, str или bytes."""
    b = _extract_bytes(key)
    if b is not None:
        return b
    # lua-table-обёртка {type=..., bytes=...}
    if hasattr(key, "items") or hasattr(key, "keys"):
        try:
            b = key["bytes"] if hasattr(key, "__getitem__") else None
        except Exception:
            b = None
        if b is not None:
            return _extract_bytes(b)
    return None


def _make_key_wrapper(pem_bytes: bytes, kind: str) -> dict:
    """{__lupi_data_key, type, bytes} — lua-таблица через init._lua_convert."""
    return {
        "__lupi_data_key": True,
        "type": kind,
        "bytes": _to_lua(pem_bytes),
    }


class DataComponent:
    def __init__(self, tier: int = 3):
        self.tier = int(tier)

    # ── базовые (tier 1+) ──────────────────────────────────────────
    def crc32(self, data, *_):
        data = _from_lua(data)
        crc = zlib.crc32(data) & 0xFFFFFFFF
        return _to_lua(crc.to_bytes(4, "big"))

    def encode64(self, data, *_):
        return _to_lua(base64.b64encode(_from_lua(data)))

    def decode64(self, data, *_):
        try:
            return _to_lua(base64.b64decode(_from_lua(data), validate=False))
        except Exception as e:
            logger.debug("decode64 failed: %s", e)
            return None, str(e)

    def md5(self, data, hmac_key=None, *_):
        data = _from_lua(data)
        if hmac_key is not None:
            return _to_lua(hmac.new(_from_lua(hmac_key), data, hashlib.md5).digest())
        return _to_lua(hashlib.md5(data).digest())

    def sha256(self, data, hmac_key=None, *_):
        data = _from_lua(data)
        if hmac_key is not None:
            return _to_lua(hmac.new(_from_lua(hmac_key), data, hashlib.sha256).digest())
        return _to_lua(hashlib.sha256(data).digest())

    def deflate(self, data, *_):
        return _to_lua(zlib.compress(_from_lua(data), 6))

    def inflate(self, data, *_):
        try:
            return _to_lua(zlib.decompress(_from_lua(data)))
        except Exception as e:
            logger.debug("inflate failed: %s", e)
            return None, str(e)

    def get_limit(self, *_):
        return DATA_LIMIT

    # ── tier >= 2 ──────────────────────────────────────────────────
    def random(self, length, *_):
        try:
            n = int(length)
        except (TypeError, ValueError):
            return None, "number expected"
        if n < 0 or n > DATA_LIMIT:
            return None, "invalid size"
        return _to_lua(os.urandom(n))

    def encrypt(self, data, key, iv, *_):
        if not _CRYPTO_OK:
            return None, "not implemented"
        data_b = _from_lua(data)
        key_b = _extract_bytes(key)
        iv_b = _extract_bytes(iv)
        if key_b is None or iv_b is None:
            return None, "invalid key or iv"
        if len(key_b) not in (16, 24, 32):
            return None, "invalid key size"
        if len(iv_b) != 16:
            return None, "invalid iv size"
        try:
            c = Cipher(algorithms.AES(key_b), modes.CFB(iv_b))
            enc = c.encryptor()
            return _to_lua(enc.update(data_b) + enc.finalize())
        except Exception as e:
            logger.debug("encrypt failed: %s", e)
            return None, str(e)

    def decrypt(self, data, key, iv, *_):
        if not _CRYPTO_OK:
            return None, "not implemented"
        data_b = _from_lua(data)
        key_b = _extract_bytes(key)
        iv_b = _extract_bytes(iv)
        if key_b is None or iv_b is None:
            return None, "invalid key or iv"
        if len(key_b) not in (16, 24, 32):
            return None, "invalid key size"
        if len(iv_b) != 16:
            return None, "invalid iv size"
        try:
            c = Cipher(algorithms.AES(key_b), modes.CFB(iv_b))
            dec = c.decryptor()
            return _to_lua(dec.update(data_b) + dec.finalize())
        except Exception as e:
            logger.debug("decrypt failed: %s", e)
            return None, str(e)

    # ── tier >= 3 ──────────────────────────────────────────────────
    def ecdsa(self, data, key, sig=None, *_):
        if not _CRYPTO_OK:
            return None, "not implemented"
        data_b = _from_lua(data)
        key_b = _extract_key_bytes(key)
        if key_b is None:
            return None, "invalid key"

        if sig is None:
            # подписать
            try:
                priv = load_pem_private_key(key_b, password=None)
            except Exception as e:
                return None, str(e)
            try:
                sig_b = priv.sign(data_b, _ec.ECDSA(_hashes.SHA256()))
                return _to_lua(sig_b)
            except Exception as e:
                return None, str(e)
        # проверить
        sig_b = _extract_bytes(sig)
        if sig_b is None:
            return None, "invalid signature"
        try:
            pub = load_pem_public_key(key_b)
        except Exception as e:
            return None, str(e)
        try:
            pub.verify(sig_b, data_b, _ec.ECDSA(_hashes.SHA256()))
            return True
        except Exception:
            return False

    def ecdh(self, priv, pub, *_):
        if not _CRYPTO_OK:
            return None, "not implemented"
        priv_b = _extract_key_bytes(priv)
        pub_b = _extract_key_bytes(pub)
        if priv_b is None or pub_b is None:
            return None, "invalid key"
        try:
            priv_obj = load_pem_private_key(priv_b, password=None)
            pub_obj = load_pem_public_key(pub_b)
        except Exception as e:
            return None, str(e)
        try:
            shared = priv_obj.exchange(_ec.ECDH(), pub_obj)
            return _to_lua(shared)
        except Exception as e:
            return None, str(e)

    def generate_key_pair(self, bit_len=256, *_):
        if not _CRYPTO_OK:
            return None, "not implemented"
        try:
            bl = int(bit_len) if bit_len is not None else 256
        except (TypeError, ValueError):
            return None, "invalid bit length"
        if bl not in (256, 384):
            return None, "invalid bit length"
        curve = _ec.SECP256R1() if bl == 256 else _ec.SECP384R1()
        try:
            priv = _ec.generate_private_key(curve)
            pub = priv.public_key()
            priv_pem = priv.private_bytes(
                Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
            pub_pem = pub.public_bytes(
                Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        except Exception as e:
            return None, str(e)
        return (_make_key_wrapper(pub_pem, "public"),
                _make_key_wrapper(priv_pem, "private"))

    def deserialize_key(self, data, key_type=None, *_):
        if not _CRYPTO_OK:
            return None, "not implemented"
        data_b = _from_lua(data) if isinstance(data, str) else bytes(data)
        kt = (key_type or "public").lower() if isinstance(key_type, str) else "public"
        try:
            if "priv" in kt or "secret" in kt:
                obj = load_pem_private_key(data_b, password=None)
                pem = obj.private_bytes(
                    Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
                return _make_key_wrapper(pem, "private")
            obj = load_pem_public_key(data_b)
            pem = obj.public_bytes(
                Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
            return _make_key_wrapper(pem, "public")
        except Exception as e:
            return None, str(e)

    def as_proxy(self) -> dict:
        p = {
            "crc32":     self.crc32,
            "encode64":  self.encode64,
            "decode64":  self.decode64,
            "md5":       self.md5,
            "sha256":    self.sha256,
            "deflate":   self.deflate,
            "inflate":   self.inflate,
            "getLimit":  self.get_limit,
        }
        if self.tier >= 2:
            p.update({
                "random":   self.random,
                "encrypt":  self.encrypt,
                "decrypt":  self.decrypt,
            })
        if self.tier >= 3:
            p.update({
                "ecdsa":           self.ecdsa,
                "ecdh":            self.ecdh,
                "generateKeyPair": self.generate_key_pair,
                "deserializeKey":  self.deserialize_key,
            })
        return p


def register(uuid: str | None = None, tier: int = 3) -> str:
    comp = DataComponent(tier=tier)
    addr = component_mod.register(uuid, "data", comp.as_proxy())
    logger.info("Data component registered: %s (tier %d)", addr, tier)
    return addr
"""
core/data.py — компонент data (base64, hashes, deflate, random).
Аналог src/component/data.lua в OCEmu.
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


class DataComponent:
    def __init__(self, tier: int = 3):
        self.tier = int(tier)

    # ── базовые (tier 1+) ──────────────────────────────────────────────
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

    # ── tier >= 2 ──────────────────────────────────────────────────────
    def random(self, length, *_):
        try:
            n = int(length)
        except (TypeError, ValueError):
            return None, "number expected"
        if n < 0 or n > DATA_LIMIT:
            return None, "invalid size"
        return _to_lua(os.urandom(n))

    def encrypt(self, *_): return None, "not implemented"
    def decrypt(self, *_): return None, "not implemented"

    # ── tier >= 3 ──────────────────────────────────────────────────────
    def ecdsa(self, *_):             return None, "not implemented"
    def ecdh(self, *_):              return None, "not implemented"
    def generate_key_pair(self, *_): return None, "not implemented"
    def deserialize_key(self, *_):   return None, "not implemented"

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
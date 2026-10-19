"""轻量口令加密流（迭代 62）：PBKDF2-HMAC-SHA256 派生 + SHA-256 CTR keystream。

诚实定位：这是**混淆级**保护（防随手翻看/误发），不是 AES 级密码学。
高敏感迁移请用 age/gpg 加密后再走普通导出。仅用标准库，无第三方依赖。

格式：magic(16B) || salt(16B) || nonce(8B) || ciphertext
"""
from __future__ import annotations

import hashlib
import hmac
import os
import struct

MAGIC = b"hippomind-enc1"  # 14 字节，补齐 16 便于头部校验
SALT_LEN = 16
NONCE_LEN = 8
PBKDF2_ITERATIONS = 200_000


def derive_key(passphrase: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"),
                               salt, PBKDF2_ITERATIONS)


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    blocks = []
    counter = 0
    while sum(len(b) for b in blocks) < length:
        blocks.append(hashlib.sha256(
            key + nonce + struct.pack(">Q", counter)).digest())
        counter += 1
    return b"".join(blocks)[:length]


def encrypt_bytes(data: bytes, passphrase: str) -> bytes:
    salt = os.urandom(SALT_LEN)
    nonce = os.urandom(NONCE_LEN)
    key = derive_key(passphrase, salt)
    ks = _keystream(key, nonce, len(data))
    ct = bytes(a ^ b for a, b in zip(data, ks))
    tag = hmac.new(key, MAGIC + salt + nonce + ct, hashlib.sha256).digest()[:16]
    return MAGIC + salt + nonce + ct + tag


def is_encrypted(blob: bytes) -> bool:
    return blob[:len(MAGIC)] == MAGIC


def decrypt_bytes(blob: bytes, passphrase: str) -> bytes:
    if not is_encrypted(blob):
        raise ValueError("不是 hippomind 加密导出格式")
    body = blob[len(MAGIC):]
    salt, nonce, ct_and_tag = (body[:SALT_LEN],
                               body[SALT_LEN:SALT_LEN + NONCE_LEN],
                               body[SALT_LEN + NONCE_LEN:])
    if len(ct_and_tag) < 16:
        raise ValueError("密文被截断")
    ct, tag = ct_and_tag[:-16], ct_and_tag[-16:]
    key = derive_key(passphrase, salt)
    expect = hmac.new(key, MAGIC + salt + nonce + ct, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(tag, expect):
        raise ValueError("口令错误或文件被篡改（HMAC 校验失败）")
    ks = _keystream(key, nonce, len(ct))
    return bytes(a ^ b for a, b in zip(ct, ks))

import pytest
import os
from meshcore_helpers import decrypt_group_text, DecryptedGroupText

# Estas utilidades permiten simular RAW payloads cifrados correctamente siguiendo el estándar MeshCore group
try:
    from Crypto.Cipher import AES
except ImportError:
    AES = None
import hmac
import hashlib

def build_test_payload(message, channel_key, timestamp=1, flags=0, sender=None):
    """Construye un payload RAW absolutamente válido de MeshCore como hace RTfMC/firmware"""
    if AES is None:
        pytest.skip("pycryptodome/AES no disponible")
    # Estructura: [channel_hash (1 byte)][mac (2 bytes)][cifrado...]
    channel_hash = bytes([123])  # Canal ficticio 0x7b (arbitrario)
    text = f"{sender}: {message}" if sender else message
    plain = timestamp.to_bytes(4, 'little') + bytes([flags]) + text.encode("utf-8")
    # Padding a múltiplos de 16 (AES block)
    padded = plain + bytes((16 - len(plain) % 16) % 16)
    cipher = AES.new(channel_key, AES.MODE_ECB)
    ciphertext = cipher.encrypt(padded)
    # canal_secret es key+16 bytes 0 (como en helper)
    channel_secret = channel_key + bytes(16)
    mac = hmac.new(channel_secret, ciphertext, hashlib.sha256).digest()[:2]
    return channel_hash + mac + ciphertext

def random_key():
    return os.urandom(16)

def test_decrypt_success():
    key = random_key()
    sender = "ALICE"
    msg = "hello world"
    payload = build_test_payload(msg, key, timestamp=77, flags=1, sender=sender)
    result = decrypt_group_text(payload, key)
    assert result
    assert result.message == msg
    assert result.sender == sender
    assert result.timestamp == 77
    assert result.flags == 1


def test_decrypt_wrong_key():
    key = random_key()
    msg = "fail case"
    payload = build_test_payload(msg, key)
    wrong_key = random_key()
    assert not decrypt_group_text(payload, wrong_key)


def test_decrypt_bad_mac():
    key = random_key()
    payload = build_test_payload("bad mac", key)
    tampered = payload[:-1] + bytes([payload[-1] ^ 0xFF]) # Toucha último byte
    assert decrypt_group_text(tampered, key) is None


def test_payload_too_short():
    key = random_key()
    assert decrypt_group_text(b"\x01\x02", key) is None
    assert decrypt_group_text(b"", key) is None


def test_payload_not_block():
    key = random_key()
    msg = "padtest"
    payload = build_test_payload(msg, key)
    # Remove one byte from ciphertext (breaks AES blocksize)
    wrong_len = payload[:-1]
    assert decrypt_group_text(wrong_len, key) is None


def test_sender_edge_cases():
    key = random_key()
    payload = build_test_payload(":test", key, sender=None)
    res = decrypt_group_text(payload, key)
    assert res.sender is None

    payload2 = build_test_payload("BOB: Hello", key, sender=None)
    res2 = decrypt_group_text(payload2, key)
    assert res2.sender == "BOB"
    assert res2.message == "Hello"

    # Nombre raro (con : dentro de los 50 primeros)
    payload3 = build_test_payload("A:[]: Hi", key, sender=None)
    res3 = decrypt_group_text(payload3, key)
    assert res3.sender is None

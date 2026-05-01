import hashlib
import hmac
import time
import re
import unicodedata

try:
    from Crypto.Cipher import AES
except ImportError:
    AES = None

from collections import namedtuple

# Minimal container for group decrypt result (like RTfMC)
DecryptedGroupText = namedtuple('DecryptedGroupText', ['timestamp', 'flags', 'sender', 'message', 'channel_hash'])

def decrypt_group_text(payload: bytes, channel_key: bytes):
    """Decrypt a MeshCore group (channel) text packet using the channel key; returns DecryptedGroupText or None"""
    if len(payload) < 3 or AES is None:
        return None
    channel_hash = format(payload[0], "02x")
    cipher_mac = payload[1:3]
    ciphertext = payload[3:]
    if len(ciphertext) == 0 or len(ciphertext) % 16 != 0:
        # AES requires 16-byte blocks
        return None
    channel_secret = channel_key + bytes(16)
    calculated_mac = hmac.new(channel_secret, ciphertext, hashlib.sha256).digest()
    if calculated_mac[:2] != cipher_mac:
        return None
    try:
        cipher = AES.new(channel_key, AES.MODE_ECB)
        decrypted = cipher.decrypt(ciphertext)
    except Exception:
        return None
    if len(decrypted) < 5:
        return None
    timestamp = int.from_bytes(decrypted[0:4], "little")
    flags = decrypted[4]
    msg_bytes = decrypted[5:]
    try:
        msg_text = msg_bytes.decode("utf-8")
        null_idx = msg_text.find("\x00")
        if null_idx >= 0:
            msg_text = msg_text[:null_idx]
    except Exception:
        return None
    sender = None
    content = msg_text
    colon_idx = msg_text.find(": ")
    if 0 < colon_idx < 50:
        candidate = msg_text[:colon_idx]
        if not any(c in candidate for c in ":[]\x00"):
            sender = candidate
            content = msg_text[colon_idx+2:]
    return DecryptedGroupText(timestamp, flags, sender, content, channel_hash)

def compute_channel_id(name: str, key: str) -> str:
    """
    Compute MeshCore channel ID compatible with Remote Terminal and standard MeshCore:
    - If 'key' is provided (not empty) and name does NOT start with '#',
      returns that key (as uppercase hex, exactly 32 hex characters) as channel_id.
    - If channel name starts with '#', or no key is provided, computes SHA-256(name.encode("utf-8")),
      takes first 16 bytes, and returns as uppercase hex (32 chars).
    """
    key = (key or '').strip()
    name = (name or '').strip()
    # Standard: If non-hashtag channel and key present, use key as channel_id
    if key and not name.startswith('#'):
        key_hex = key.upper()
        if len(key_hex) == 32 and all(c in "0123456789ABCDEF" for c in key_hex):
            return key_hex
        raise ValueError(f"Channel key must be 32 hex characters. Got: {key!r}")
    # Otherwise (hashtag/public or missing key), derive from name
    hash16 = hashlib.sha256(name.encode('utf-8')).digest()[:16]
    return hash16.hex().upper()

# --- MeshCore send helpers ---

async def send_channel_message_with_timestamp(mc, channel_index, message):
    """
    Send a message to MeshCore channel **index** (slot), adding a unique timestamp prefix.
    Sanitizes message to remove control/non-printable characters reliably.
    channel_index: Integer index (slot) of the MeshCore channel (not channel_id/hash/key).
    Returns the result of mc.commands.send_msg(channel_index, message).
    Pure helper, does not depend on plugin or logging.
    """
    message = sanitize_text(message)
    timestamp_ms = int(time.time() * 1000)
    prefix = f"[{timestamp_ms:x}] "
    outgoing_with_ts = prefix + message
    return await mc.commands.send_msg(channel_index, outgoing_with_ts)

_timestamp_regex = re.compile(r"^\[([0-9a-f]+)\] (.*)")
def has_timestamp_prefix(text):
    m = _timestamp_regex.match(text or "")
    return bool(m)

# --- Sanitization helper ---
_MAX_MSG_LEN = 512  # Bytes, adjust if protocol requires

def sanitize_text(text: str) -> str:
    """
    Strongly sanitize a string to be safe for MeshCore and Matrix interoperable systems:
    - Remove non-printable/control characters (except ASCII newlines if needed)
    - Normalize unicode
    - Replace or strip suspicious whitespace (tabs, etc)
    - Trim trailing/leading whitespace (optional)
    - Truncate message to _MAX_MSG_LEN bytes utf-8, append '…' if cut
    Always returns str, never None
    """
    if text is None:
        return ""
    # Normalize unicode
    text = unicodedata.normalize("NFKC", str(text))
    # Remove C0/C1 controls except \n
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", text)
    # Remove tabs explicitly
    text = text.replace("\t", " ")
    # Remove zero width joiner/non-joiner
    text = re.sub(r"[\u200b-\u200f\u202a-\u202e\ufeff]", "", text)
    # Strip leading/trailing spaces/tabs/etc, but not newlines
    text = text.strip(" \t\r\f\v")
    # Truncate to _MAX_MSG_LEN bytes (utf-8 safe, ellipsis if cut)
    encoded = text.encode("utf-8")
    if len(encoded) > _MAX_MSG_LEN:
        # Truncate on character boundary within byte limit
        while len(text.encode("utf-8")) > _MAX_MSG_LEN - 3:
            text = text[:-1]
        text += "…"
    return text


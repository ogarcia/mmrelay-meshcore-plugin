# SPDX-License-Identifier: GPL-3.0-or-later
#
# mmrelay-meshcore-plugin — MeshCore ↔ Matrix relay community plugin
# Copyright (C) 2026 Óscar García Amor
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""
mmrelay-meshcore-plugin

Community plugin for meshtastic-matrix-relay (mmrelay) that
bridges Matrix rooms with MeshCore radio network channels, analogous
to how mmrelay bridges Meshtastic.

Relay directions:
  - MeshCore → Matrix: channel messages and direct messages forwarded to Matrix rooms.
  - Matrix → MeshCore: messages from mapped Matrix rooms sent to MeshCore channels.

Requires mmrelay >= 1.4 and meshcore >= 2.3.7.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import sqlite3
import time
from typing import Any

# Crypto dependencies for RAW MeshCore group decryption
import hashlib
import hmac
try:
    from Crypto.Cipher import AES
except ImportError:
    AES = None

from meshcore_helpers import decrypt_group_text, DecryptedGroupText, sanitize_text

try:
    from nio import MatrixRoom, RoomMessageText
except ImportError:
    pass  # resolved at runtime inside mmrelay's environment

try:
    from mmrelay.log_utils import get_logger
    from mmrelay.plugin_loader import PLUGIN_STATE_FILENAME
    from mmrelay.plugins.base_plugin import BasePlugin
except ImportError:
    from plugins.base_plugin import BasePlugin  # type: ignore[no-redef]
    PLUGIN_STATE_FILENAME = ".mmrelay-plugin-state.json"
    # fallback, matches mmrelay default

# Max MeshCore radio message length (bytes).
# Most firmware variants cap at ~200 bytes.
_MAX_MSG_LEN = 200


from meshcore_helpers import compute_channel_id


from meshcore_helpers import compute_channel_id

# Standard public channel keys for hashtag channels.
PREDEFINED_PUBLIC_KEYS = {
    "public": "8b3387e9c5cdea6ac9e5edbaa115cd72",
    # (Extend here for future public hashtags)
}

def parse_channel_mapping(mapping: dict) -> dict | None:
    """Parse a channel mapping entry from config.

    Supports:
        matrix_room: "!roomid..."
        meshcore_channel_name: "GALICIA"
        meshcore_channel_key: "ABC123..."
        meshcore_channel_index: 1   # (OPTIONAL)

    Returns a dict with: matrix_room, channel_name, channel_key, channel_id, channel_index (if any).
    Returns None if config is invalid.
    """
    room = mapping.get("matrix_room")
    name = mapping.get("meshcore_channel_name")
    key = mapping.get("meshcore_channel_key")
    index = mapping.get("meshcore_channel_index")

    if not room or not name:
        return None

    # If channel is public (starts with '#'), canonicalize the name and fill standard key if empty
    canonical_name = name.lstrip('#').strip() if name else name
    # For hashtag/public channel with empty key, auto-fill the well-known key (if registered)
    if name.startswith('#') and (not key):
        canonical_lc = canonical_name.lower()
        if canonical_lc in PREDEFINED_PUBLIC_KEYS:
            key = PREDEFINED_PUBLIC_KEYS[canonical_lc]
    result = {
        "matrix_room": room,
        "channel_name": canonical_name,  # Only canonical stored
        "channel_key": key,  # Always filled for known publics
        "channel_id": compute_channel_id(canonical_name, key),
    }
    if index is not None:
        try:
            result["channel_index"] = int(index)
        except Exception:
            pass  # Ignore, log/warn can be added if needed
    return result



class Plugin(BasePlugin):
    """Bridge between Matrix rooms and MeshCore radio channels."""

    plugin_name = "meshcore-matrix-relay"

    # ── Init ──────────────────────────────────────────────────────────────────

    def __init__(self) -> None:
        super().__init__()
        # MeshCore connection instance (set when listener is running).
        self._mc: Any = None
        # Future returned by run_coroutine_threadsafe for the listener coroutine.
        self._listener_future: Any = None
        # Guard against registering the Matrix callback more than once.
        self._matrix_callback_registered = False
        # Discovered channels: name -> {channel_id, key, idx (if known)}
        self._channels_by_name: dict[str, dict] = {}
        # Index lookup: channel_idx -> {channel_name, channel_id, ...}
        self._channels_by_idx: dict[int, dict] = {}
        # Reverse: channel_id (64 hex) -> name
        self._channel_id_to_name: dict[str, str] = {}
        # Pending MeshCore messages awaiting slot mapping (idx: list of (timestamp, msg_dict))
        self._pending_slot_messages: dict[int, list[tuple[float, dict]]] = {}
        self._init_db()

    # ── DB helpers ────────────────────────────────────────────────────────────

    def _db_connect(self) -> sqlite3.Connection:
        from mmrelay.db_utils import get_db_path

        conn = sqlite3.connect(get_db_path())
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        """Create the meshcore_contacts table if it does not already exist."""
        try:
            with self._db_connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS meshcore_contacts (
                        pubkey_prefix  TEXT PRIMARY KEY,
                        adv_name       TEXT,
                        last_advert    INTEGER,
                        lat            REAL,
                        lon            REAL,
                        last_seen      INTEGER
                    )
                    """
                )
        except Exception as exc:
            self.logger.error("Failed to initialise meshcore_contacts table: %s", exc)

    def _upsert_contacts(self, contacts: Any) -> None:
        """Insert or update a list/iterable of contact dicts into the DB."""
        now = int(time.time())
        rows = []
        for c in contacts:
            pubkey = c.get("public_key", "")
            if not pubkey:
                continue
            rows.append(
                (
                    pubkey,
                    c.get("adv_name") or "",
                    c.get("last_advert", now),
                    c.get("adv_lat") or c.get("lat"),
                    c.get("adv_lon") or c.get("lon"),
                    now,
                )
            )
        if not rows:
            return
        try:
            with self._db_connect() as conn:
                conn.executemany(
                    """
                    INSERT INTO meshcore_contacts
                        (pubkey_prefix, adv_name, last_advert, lat, lon, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(pubkey_prefix) DO UPDATE SET
                        adv_name    = CASE WHEN excluded.adv_name != ''
                                          THEN excluded.adv_name
                                          ELSE adv_name END,
                        last_advert = COALESCE(excluded.last_advert, last_advert),
                        lat         = COALESCE(excluded.lat, lat),
                        lon         = COALESCE(excluded.lon, lon),
                        last_seen   = excluded.last_seen
                    """,
                    rows,
                )
        except Exception as exc:
            self.logger.error("Failed to upsert MeshCore contacts: %s", exc)

    def _touch_contact(self, pubkey: str) -> None:
        """Update last_seen timestamp for an already-known contact."""
        try:
            with self._db_connect() as conn:
                conn.execute(
                    "UPDATE meshcore_contacts SET last_seen = ? WHERE pubkey_prefix = ?",
                    (int(time.time()), pubkey),
                )
        except Exception as exc:
            self.logger.debug("Could not touch contact %s: %s", pubkey[:8], exc)

    def _lookup_name_by_prefix(self, hex_prefix: str) -> str | None:
        """
        Return adv_name for the contact whose pubkey_prefix starts with hex_prefix.

        hex_prefix should be a lowercase hex string (e.g. 8 chars for 4-byte transport code
        or 12 chars for 6-byte pubkey_prefix from CONTACT_MSG_RECV).

        Returns None if the prefix matches zero or more than one contact (ambiguous).
        """
        if not hex_prefix:
            return None
        plen = len(hex_prefix)
        try:
            with self._db_connect() as conn:
                rows = conn.execute(
                    "SELECT adv_name FROM meshcore_contacts "
                    "WHERE SUBSTR(pubkey_prefix, 1, ?) = ? LIMIT 2",
                    (plen, hex_prefix.lower()),
                ).fetchall()
            if len(rows) != 1:
                if len(rows) > 1:
                    self.logger.debug(
                        "Ambiguous prefix %s matches %d contacts; sender unknown",
                        hex_prefix[:8],
                        len(rows),
                    )
                return None
            return rows[0]["adv_name"] if rows[0]["adv_name"] else None
        except Exception as exc:
            self.logger.debug("DB lookup failed for prefix %s: %s", hex_prefix[:8], exc)
            return None

    # ── Config helpers ────────────────────────────────────────────────────────

    def _channel_mappings(self) -> list[dict]:
        """Return validated channel mappings (only named channels with PSK)."""
        raw = self.config.get("channel_mappings") or []
        result = []
        for m in raw:
            parsed = parse_channel_mapping(m)
            if parsed:
                result.append(parsed)
            else:
                self.logger.warning("Invalid channel mapping ignored: %s", m)
        return result

    def _get_matrix_room_for_channel_name(self, channel_name: str) -> str | None:
        """Find Matrix room for a channel name."""
        for ch in self._channel_mappings():
            if ch["channel_name"] == channel_name:
                return ch["matrix_room"]
        return None

    def _get_channel_info_for_room(self, room_id: str) -> dict | None:
        """Get channel info dict for a Matrix room.

        Returns dict with 'channel_name', 'channel_id', 'channel_key', 'matrix_room'.
        """
        for ch in self._channel_mappings():
            if ch["matrix_room"] == room_id:
                return ch
        return None

    def _dm_room(self) -> str | None:
        return self.config.get("direct_message_room")

    def _mesh_name(self) -> str:
        return self.config.get("mesh_name", "MeshCore")

    def _fmt_channel_prefix(self, channel_info: dict) -> str:
        """Format prefix for channel messages.

        Args:
            channel_info: dict with 'type' ('numeric'/'named'), 'channel_idx' (int),
                         'channel_name' (str, for named channels)
        """
        if not self.config.get("channel_prefix_enabled", False):
            return ""
        fmt = self.config.get("channel_prefix_format", "[{mesh}]: ")

        if channel_info.get("type") == "named":
            channel_display = channel_info.get("channel_name", "?")
        else:
            channel_display = channel_info.get("channel_idx", "?")

        return fmt.format(
            mesh=self._mesh_name(),
            channel=channel_display,
        )

    def _fmt_dm_prefix(self, sender: str | None, pubkey_short: str) -> str:
        if not self.config.get("dm_prefix_enabled", True):
            return ""
        fmt = self.config.get("dm_prefix_format", "[DM] {sender}({pubkey}): ")
        return fmt.format(
            sender=sender or pubkey_short,
            pubkey=pubkey_short,
            mesh=self._mesh_name(),
        )

    def _fmt_matrix_prefix(self, display_name: str) -> str:
        if not self.config.get("matrix_prefix_enabled", True):
            return ""
        fmt = self.config.get("matrix_prefix_format", "{display}[M]: ")
        return fmt.format(display=display_name)

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        super().start()
        self._log_config()

        # Verify meshcore is importable in this thread (where mmrelay has already
        # set up sys.path).  If not, clear the mmrelay plugin install-state cache
        # so that the next startup triggers a fresh requirements installation.
        try:
            importlib.invalidate_caches()
            import meshcore  # noqa: F401  # type: ignore[import-untyped]
        except ImportError:
            self._clear_plugin_install_state()
            self.logger.error(
                "meshcore package not found — plugin install cache cleared. "
                "Restart mmrelay to trigger automatic reinstallation."
            )
            return

        try:
            from mmrelay import meshtastic_utils  # type: ignore[attr-defined]

            loop = meshtastic_utils.event_loop
            if loop is None:
                self.logger.error(
                    "asyncio event loop not available; MeshCore listener will not start"
                )
                return
            self._listener_future = asyncio.run_coroutine_threadsafe(
                self._run_listener_loop(), loop
            )
            self._listener_future.add_done_callback(self._on_listener_done)
            self.logger.info("MeshCore listener task scheduled on event loop")
        except Exception as exc:
            self.logger.error("Failed to start MeshCore listener: %s", exc)

    def _clear_plugin_install_state(self) -> None:
        """Remove the mmrelay plugin install-state cache file for this plugin.

        mmrelay skips requirements reinstallation when the cache records a
        matching commit SHA.  Deleting the file forces a reinstall on the next
        startup, which is the correct recovery when the deps directory has been
        wiped (e.g. after a container restart with ephemeral storage).
        """
        try:
            plugin_dir = os.path.dirname(os.path.abspath(__file__))
            state_file = os.path.join(plugin_dir, PLUGIN_STATE_FILENAME)
            if os.path.exists(state_file):
                os.remove(state_file)
                self.logger.info("Removed plugin state cache: %s", state_file)
        except Exception as exc:
            self.logger.debug("Could not remove plugin state cache: %s", exc)

    def _log_config(self) -> None:
        """Log startup summary of the active configuration."""
        conn_cfg: dict = self.config.get("connection") or {}
        conn_type: str = conn_cfg.get("type", "tcp")

        if conn_type == "tcp":
            target = f"{conn_cfg.get('host', 'localhost')}:{conn_cfg.get('port', 5000)}"
        elif conn_type == "serial":
            target = conn_cfg.get("serial_port", "/dev/ttyUSB0")
        elif conn_type == "ble":
            target = conn_cfg.get("ble_address", "<scan>")
        else:
            target = "?"

        mappings = self._channel_mappings()
        dm_room = self._dm_room()

        self.logger.info("MeshCore: MeshCore ↔ Matrix  (mesh: %s)", self._mesh_name())
        self.logger.info("  Connection : %s  →  %s", conn_type.upper(), target)

        if mappings:
            self.logger.info("  MeshCore Channels ↔ Matrix Rooms (%d configured):", len(mappings))
            for ch in mappings:
                room = ch.get("matrix_room", "?")
                name = ch.get("channel_name", "?")
                key = ch.get("channel_key", "")
                if key:
                    key_display = f"{key[:4]}...{key[-4:]}" if len(key) > 8 else key
                else:
                    key_display = "Public/Unknown"
                self.logger.info("    %s (key: %s)  →  %s", name, key_display, room)
        else:
            self.logger.warning("  ⚠️  No channel_mappings configured — relay inactive")

        if dm_room:
            self.logger.info("  DM room    : %s  (receive-only)", dm_room)

        ch_fmt = self.config.get("channel_prefix_format", "[{mesh}]: ")
        dm_fmt = self.config.get("dm_prefix_format", "[DM] {sender}({pubkey}): ")
        mx_fmt = self.config.get("matrix_prefix_format", "{display}[M]: ")
        self.logger.info("  Prefix (MeshCore→Matrix channel) : %r", ch_fmt)
        self.logger.info("  Prefix (MeshCore→Matrix DM)      : %r", dm_fmt)
        self.logger.info("  Prefix (Matrix→MeshCore)         : %r", mx_fmt)

    def on_stop(self) -> None:
        if self._listener_future and not self._listener_future.done():
            self._listener_future.cancel()
        self._listener_future = None

        mc = self._mc
        if mc is not None:
            self._mc = None
            try:
                from mmrelay import meshtastic_utils  # type: ignore[attr-defined]

                loop = meshtastic_utils.event_loop
                if loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(mc.disconnect(), loop)
            except Exception as exc:
                self.logger.debug("Error scheduling MeshCore disconnect: %s", exc)

    def _on_listener_done(self, future: Any) -> None:
        try:
            exc = future.exception()
            if exc:
                self.logger.error(
                    "MeshCore listener exited with unhandled exception: %s",
                    exc, exc_info=exc,
                )
        except Exception:
            pass

    # ── MeshCore background listener ──────────────────────────────────────────

    async def _run_listener_loop(self) -> None:
        """Main reconnection and message relay loop (runs after meshcore is importable)."""
        await self._setup_matrix_callback()

        from meshcore.events import EventType  # type: ignore[import-untyped]

        conn_cfg: dict = self.config.get("connection") or {}
        conn_type: str = conn_cfg.get("type", "tcp")
        reconnect_delay = 30

        if conn_type == "tcp":
            target = f"{conn_cfg.get('host', 'localhost')}:{conn_cfg.get('port', 5000)}"
        elif conn_type == "serial":
            target = conn_cfg.get("serial_port", "/dev/ttyUSB0")
        elif conn_type == "ble":
            target = conn_cfg.get("ble_address", "<scan>")
        else:
            target = "?"

        connect_timeout = 30

        while not self._stop_event.is_set():
            mc = None
            try:
                self.logger.info("Connecting to MeshCore device (%s → %s)…", conn_type.upper(), target)
                mc = await asyncio.wait_for(
                    self._connect_meshcore(conn_cfg),
                    timeout=connect_timeout,
                )
                if mc is None:
                    self.logger.error(
                        "Could not connect to MeshCore; retrying in %ss", reconnect_delay
                    )
                    await asyncio.sleep(reconnect_delay)
                    continue

                self._mc = mc
                self.logger.info("✅ Connected to MeshCore device (%s → %s)", conn_type.upper(), target)

                mc.subscribe(EventType.CONTACTS, self._on_contacts)
                mc.subscribe(EventType.NEW_CONTACT, self._on_new_contact)
                mc.subscribe(EventType.ADVERTISEMENT, self._on_advertisement)
                mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_msg)
                mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_contact_msg)
                mc.subscribe(EventType.CHANNEL_INFO, self._on_channel_info)

                # Subscribe to RAW_DATA to log undecodable packets (RTfMC-style)
                mc.subscribe(EventType.RAW_DATA, self._on_raw_data)

                # Clear and reload channels on reconnect
                self._channels_by_name.clear()
                self._channel_id_to_name.clear()
                self._channels_by_idx.clear()

                # Populate contacts on startup.
                await mc.ensure_contacts()

                # Proactively scan all slots like the standalone slot listing script.
                class _EventLike:
                    def __init__(self, payload):
                        self.payload = payload
                self.logger.info("Proactively scanning all MeshCore slots for channel info (non-event driven)")
                for idx in range(32):
                    try:
                        info = await mc.commands.get_channel(idx)
                        if info and hasattr(info, "payload") and info.payload.get('channel_name'):
                            payload = info.payload
                            name = payload.get('channel_name', '')
                            secret = payload.get('channel_secret', b"")
                            key_hex = secret.hex() if isinstance(secret, (bytes, bytearray)) else str(secret)
                            channel_id = compute_channel_id(name, key_hex)
                            await self._on_channel_info(_EventLike(payload))
                    except Exception:
                        continue

                # Drain all messages already queued in the node before going live.
                # start_auto_message_fetching() only calls get_msg() once at startup
                # and relies on MESSAGES_WAITING to fetch the rest, which means
                # pending messages only arrive when new traffic triggers that event.
                # We drain the full queue here instead.
                self.logger.info("Draining pending messages from MeshCore node…")
                drained = 0
                while not self._stop_event.is_set():
                    result = await mc.commands.get_msg()
                    if result is None or result.type in (EventType.NO_MORE_MSGS, EventType.ERROR):
                        break
                    drained += 1
                self.logger.info("Drained %d pending message(s) from node", drained)

                # Subscribe to MESSAGES_WAITING to keep fetching messages as they arrive.
                async def _on_messages_waiting(_event: Any) -> None:
                    while not self._stop_event.is_set():
                        res = await mc.commands.get_msg()
                        if res is None or res.type in (EventType.NO_MORE_MSGS, EventType.ERROR):
                            break

                mc.subscribe(EventType.MESSAGES_WAITING, _on_messages_waiting)

                self.logger.info("MeshCore relay running — listening for messages")

                # Wait until stopped or disconnected.
                while not self._stop_event.is_set():
                    if not mc.is_connected:
                        self.logger.warning("❌ MeshCore device disconnected; will reconnect in %ss", reconnect_delay)
                        break
                    await asyncio.sleep(5)

            except asyncio.CancelledError:
                self.logger.info("MeshCore listener cancelled")
                break
            except asyncio.TimeoutError:
                self.logger.error(
                    "❌ MeshCore connection timed out after %ss — retrying in %ss",
                    connect_timeout, reconnect_delay,
                )
                await asyncio.sleep(reconnect_delay)
            except Exception as exc:
                self.logger.error("❌ MeshCore connection error: %s — retrying in %ss", exc, reconnect_delay, exc_info=True)
                await asyncio.sleep(reconnect_delay)
            finally:
                if mc is not None:
                    try:
                        await mc.disconnect()
                    except Exception:
                        pass
                self._mc = None

        self.logger.info("MeshCore listener stopped")

    async def _connect_meshcore(self, conn_cfg: dict) -> Any:
        from meshcore import MeshCore  # type: ignore[import-untyped]

        conn_type: str = conn_cfg.get("type", "tcp")

        # auto_reconnect and max_reconnect_attempts are intentionally not
        # user-configurable: our own reconnection loop always retries
        # indefinitely, so whatever the library does here is just a short
        # burst before we take over.
        try:
            if conn_type == "tcp":
                return await MeshCore.create_tcp(
                    host=conn_cfg.get("host", "localhost"),
                    port=conn_cfg.get("port", 5000),
                    auto_reconnect=True,
                    max_reconnect_attempts=3,
                )
            if conn_type == "serial":
                return await MeshCore.create_serial(
                    port=conn_cfg.get("serial_port", "/dev/ttyUSB0"),
                    auto_reconnect=True,
                    max_reconnect_attempts=3,
                )
            if conn_type == "ble":
                return await MeshCore.create_ble(
                    address=conn_cfg.get("ble_address"),
                    auto_reconnect=True,
                    max_reconnect_attempts=3,
                )
            self.logger.error("Unknown MeshCore connection type: %s", conn_type)
            return None
        except Exception as exc:
            self.logger.debug("MeshCore connection attempt failed: %s", exc)
            return None

    # ── MeshCore event handlers ───────────────────────────────────────────────

    async def _on_contacts(self, event: Any) -> None:
        contacts = event.payload
        if isinstance(contacts, dict):
            self._upsert_contacts(contacts.values())
            self.logger.info("MeshCore contacts synced: %d known nodes", len(contacts))

    async def _on_new_contact(self, event: Any) -> None:
        contact = event.payload
        if isinstance(contact, dict) and contact.get("public_key"):
            self._upsert_contacts([contact])
            name = contact.get("adv_name") or "?"
            self.logger.info(
                "🆕 New MeshCore contact: %s  (key: %s…)", name, contact["public_key"][:8]
            )

    async def _on_advertisement(self, event: Any) -> None:
        """A node advertised its presence.  Update last_seen and refresh contacts."""
        pubkey = event.payload.get("public_key", "")
        if pubkey:
            self._touch_contact(pubkey)
            mc = self._mc
            if mc is not None:
                try:
                    await mc.ensure_contacts(follow=True)
                except Exception as exc:
                    self.logger.debug("ensure_contacts after advertisement failed: %s", exc)

    async def _on_raw_data(self, event: Any) -> None:
        """
        Handle RAW_DATA events: attempt MeshCore group decryption using all known channel keys.
        If decryption succeeds, re-emit as a channel Matrix message; otherwise log at debug.

        This brings RTfMC-style real-time decryption of channel (group) RAW packets to Matrix.
        """
        raw_bytes = event.payload if isinstance(event.payload, (bytes, bytearray)) else None
        if not raw_bytes:
            self.logger.debug("RAW_DATA event payload not bytes—ignoring")
            return
        decrypted = None
        chan_name = None
        chan_info = None
        for name, chan in self._channels_by_name.items():
            key_hex = chan.get("channel_key")
            if not key_hex or len(key_hex) != 32 and len(key_hex) != 64:
                continue
            try:
                key = bytes.fromhex(key_hex)
            except Exception:
                self.logger.debug(f"Invalid channel key hex for '{name}': {key_hex}")
                continue
            result = decrypt_group_text(raw_bytes, key)
            if result:
                decrypted = result
                chan_name = name
                chan_info = chan
                break
        if not decrypted or not chan_name:
            self.logger.debug("RAW_DATA could not be decrypted with any channel key (payload_len=%d)", len(raw_bytes))
            return
        matrix_room = self._get_matrix_room_for_channel_name(chan_name)
        if not matrix_room:
            self.logger.debug("RAW_DATA decrypted but channel '%s' is unmapped to Matrix; message ignored", chan_name)
            return
        # Compose relay message like _on_channel_msg
        channel_info = {"type": "named", "channel_name": chan_name}
        prefix = self._fmt_channel_prefix(channel_info)
        body = decrypted.message
        if decrypted.sender:
            body = f"{decrypted.sender}: {body}"
        msg = prefix + body
        self.logger.info("MeshCore RAW→Matrix [%s]: %s", chan_name, msg[:80])
        await self.send_matrix_message(matrix_room, msg)

    # ^^^^ This handler was added to provide RTfMC-grade packet decoding for RAW_DATA

    async def _on_channel_info(self, event: Any) -> None:
        """Handle CHANNEL_INFO events to auto-discover channel names and keys."""
        payload = event.payload
        name = payload.get("channel_name", "")
        secret = payload.get("channel_secret", b"")
        idx = payload.get("channel_idx")

        if not name or not secret:
            return

        key_hex = secret.hex()
        channel_id = compute_channel_id(name, key_hex)

        # Avoid duplicate mapping & logging: only proceed if this idx and name are new
        if (idx is not None and idx in self._channels_by_idx) or name in self._channels_by_name:
            return

        # Store by canonical name only (internal)
        entry = {
            "channel_name": name,
            "channel_key": key_hex,
            "channel_id": channel_id,
            "channel_idx": idx,
        }
        self._channels_by_name[name] = entry
        # Store by index for quick lookup
        if idx is not None:
            self._channels_by_idx[idx] = entry
        # Store reverse mapping
        self._channel_id_to_name[channel_id] = name

        # Robust and unique logging for channel discovery:
        if not key_hex:
            self.logger.info("Discovered PUBLIC MeshCore channel: %s (idx=%s, id=%s...)", name, idx, channel_id[:8])
        else:
            self.logger.info("Discovered MeshCore channel: %s (idx=%s, id=%s..., key=%s)", name, idx, channel_id[:8], key_hex)

        # Replay and clear any buffered messages for this slot now that mapping is known
        if idx is not None:
            pending = self._pending_slot_messages.pop(idx, [])
            if pending:
                self.logger.info(
                    "Replaying %d previously buffered MeshCore message(s) for slot idx=%d now that mapping is available.",
                    len(pending), idx)
                for ts, msg in pending:
                    await self._on_channel_msg(type('E', (), {'payload': msg})())

    async def _on_channel_msg(self, event: Any) -> None:
        msg = event.payload
        channel_idx: int = msg.get("channel_idx", -1)
        text: str = (msg.get("text") or "").strip()

        if not text:
            return

        # Try to resolve channel name from auto-discovered channels
        channel_name = None
        if channel_idx in self._channels_by_idx:
            channel_name = self._channels_by_idx[channel_idx].get("channel_name")

        # If not found by idx, try to infer from message content
        # MeshCore clients prepend "ChannelName: message" to channel messages
        if not channel_name and ": " in text:
            potential_name = text.split(": ", 1)[0].strip()
            # Check if this name is in our config
            if self._get_matrix_room_for_channel_name(potential_name):
                channel_name = potential_name
                self.logger.debug("Inferred channel name from message: %s", channel_name)

        if not channel_name:
            # Buffer message if mapping for slot index (channel_idx) is not yet known
            ts = time.time()
            if channel_idx not in self._pending_slot_messages:
                self._pending_slot_messages[channel_idx] = []
            self._pending_slot_messages[channel_idx].append((ts, msg))
            self.logger.info(
                "Channel idx=%d message buffered (no CHANNEL_INFO/mapping yet); will retry when mapping is known.",
                channel_idx,
            )
            return

        matrix_room = self._get_matrix_room_for_channel_name(channel_name)
        if not matrix_room:
            self.logger.debug("Channel %s message dropped (no Matrix room mapped)", channel_name)
            return

        channel_info = {"type": "named", "channel_name": channel_name}
        prefix = self._fmt_channel_prefix(channel_info)
        full_msg = prefix + text

        self.logger.info("MeshCore→Matrix [%s]: %s", channel_name, text[:80])
        await self.send_matrix_message(matrix_room, full_msg)

    async def _on_contact_msg(self, event: Any) -> None:
        msg = event.payload
        text: str = (msg.get("text") or "").strip()
        pubkey_prefix: str = msg.get("pubkey_prefix", "")

        if not text:
            return

        dm_room = self._dm_room()
        if not dm_room:
            self.logger.debug(
                "DM from %s dropped (no direct_message_room configured)", pubkey_prefix[:8]
            )
            return

        sender_name = self._lookup_name_by_prefix(pubkey_prefix) or None
        prefix = self._fmt_dm_prefix(sender_name, pubkey_prefix[:8])
        full_msg = prefix + text

        self.logger.info(
            "MeshCore→Matrix [DM] %s: %s", sender_name or pubkey_prefix[:8], text[:80]
        )
        await self.send_matrix_message(dm_room, full_msg)

    async def _setup_matrix_callback(self) -> None:
        """Wait for Matrix client, register our room callback and join plugin rooms.

        Called once at listener startup. Idempotent — skips if already registered.
        matrix_client may be None at plugin start because mmrelay loads plugins
        before connecting to Matrix, so we poll until it is available.
        """
        if self._matrix_callback_registered:
            return

        from mmrelay.matrix_utils import matrix_client as _mc_ref

        # Poll until matrix_client is populated (up to 120 s).
        mc_client = _mc_ref
        if mc_client is None:
            self.logger.debug("Waiting for Matrix client to become available…")
            for _ in range(120):
                if self._stop_event.is_set():
                    return
                await asyncio.sleep(1)
                from mmrelay.matrix_utils import matrix_client as _mc_ref2
                mc_client = _mc_ref2
                if mc_client is not None:
                    break

        if mc_client is None:
            self.logger.error("Matrix client never became available; Matrix→MeshCore relay disabled")
            return

        from nio import RoomMessageText  # type: ignore[import-untyped]
        mc_client.add_event_callback(self._on_matrix_room_message, RoomMessageText)
        self._matrix_callback_registered = True
        self.logger.info("Matrix room message callback registered")

        # Join all rooms defined in channel_mappings so the bot receives events.
        try:
            from mmrelay.matrix_utils import join_matrix_room  # type: ignore[attr-defined]
        except ImportError:
            self.logger.warning("join_matrix_room not available; rooms may not be joined")
            return

        for mapping in self._channel_mappings():
            room_id = mapping.get("matrix_room")
            if room_id:
                try:
                    await join_matrix_room(mc_client, room_id)
                    self.logger.debug("Joined Matrix room %s", room_id)
                except Exception as exc:
                    self.logger.warning("Could not join Matrix room %s: %s", room_id, exc)

    async def _on_matrix_room_message(self, room: Any, event: Any) -> None:
        """Relay a Matrix message to the corresponding MeshCore channel.

        Registered directly on the nio client so it fires for all rooms the bot
        has joined, independently of mmrelay's matrix_rooms configuration.
        """
        try:
            from mmrelay.matrix_utils import bot_start_time, bot_user_id  # type: ignore[attr-defined]
            from nio import RoomMessageText  # type: ignore[import-untyped]
        except ImportError:
            return

        if not isinstance(event, RoomMessageText):
            self.logger.debug("Ignored non-text event from room %s", room.room_id)
            return
        if event.sender == bot_user_id:
            self.logger.debug("Ignored message sent by the bot itself in room %s", room.room_id)
            return
        if event.server_timestamp < bot_start_time:
            self.logger.debug("Ignored backlog message (before bot start) in room %s", room.room_id)
            return

        channel_info = self._get_channel_info_for_room(room.room_id)
        if channel_info is None:
            self.logger.warning(
                "No MeshCore mapping found for Matrix room %s; message discarded.",
                room.room_id,
            )
            return

        from meshcore_helpers import sanitize_text
        try:
            display_name = room.user_name(event.sender) or event.sender
        except Exception:
            display_name = event.sender
        if not display_name:
            display_name = event.sender
        display_name = sanitize_text(display_name)

        body = event.body or ""
        reply_to = await self._resolve_matrix_reply_target(room.room_id, event)
        if reply_to:
            body = f"@[{reply_to}] {body}"
        body = sanitize_text(body)
        self.logger.debug("Preparing MeshCore relay: prefix=%r body=%r", self._fmt_matrix_prefix(display_name), body)
        outgoing = sanitize_text(self._fmt_matrix_prefix(display_name) + body)
        if not outgoing.strip():
            self.logger.warning(
                "Message from %s in room %s sanitized to empty string; not relayed to MeshCore.",
                event.sender, room.room_id)
            return

        mc = self._mc
        if mc is None or not mc.is_connected:
            self.logger.warning(
                "MeshCore not connected; dropping Matrix message from %s", room.room_id
            )
            return

        try:
            from meshcore.events import EventType  # type: ignore[import-untyped]

            self.logger.info(
                "Relaying Matrix→MeshCore: room=%s user=%s display_name=%s slot=%s channel=%s original=%r outgoing=%r",
                room.room_id,
                event.sender,
                display_name,
                channel_info.get("channel_index"),
                channel_info.get("channel_name"),
                event.body,
                outgoing,
            )
            # Only named channels with PSK supported
            result = await self._send_channel_message_with_overrides(mc, channel_info, outgoing, display_name)

        except Exception as exc:
            self.logger.error(
                "Failed to send to MeshCore channel %s: %s",
                channel_info.get("channel_name", "?"),
                exc,
            )

    # ── Buffer Cleanup Utility ───────────────────────────────────────────────

    async def _cleanup_pending_slot_messages(self):
        """
        Discard pending buffered slot messages that have been waiting too long (default: >5 seconds).
        This prevents memory leaks/race; warn for any lost messages.
        """
        now = time.time()
        TIMEOUT = 5.0
        stale_slots = []
        for idx, lst in self._pending_slot_messages.items():
            still_valid = []
            for ts, msg in lst:
                if now - ts > TIMEOUT:
                    self.logger.warning(
                        "Slot idx=%d message dropped after %0.1fs pending (mapping/channel_info never received): %r",
                        idx, now - ts, msg)
                else:
                    still_valid.append((ts, msg))
            if still_valid:
                self._pending_slot_messages[idx] = still_valid
            else:
                stale_slots.append(idx)
        for idx in stale_slots:
            del self._pending_slot_messages[idx]

    # ── Matrix → MeshCore ─────────────────────────────────────────────────────

    async def _send_channel_message_with_overrides(self, mc, channel_info, outgoing, display_name):
        """
        Robust send to MeshCore channel, using correct slot index (channel_index).
        1. Get the channel slot index from channel_info or autodiscover if absent.
        2. Send the message using that index.
        3. Log and abort if the index cannot be found.

        Args:
            mc: MeshCore connection instance.
            channel_info: Dict with channel metadata (must have 'channel_id', 'channel_name', optionally 'channel_index').
            outgoing: Message to send (str).
            display_name: Sender display name for logging.
        """
        from meshcore.events import EventType  # type: ignore[import-untyped]
        from meshcore_helpers import send_channel_message_with_timestamp

        raw_name = channel_info.get("channel_name", "?")
        canonical_name = raw_name.lstrip('#').strip() if raw_name else raw_name
        channel_index = channel_info.get("channel_index")
        if channel_index is None:
            # Always recompute the canonical channel_id for robust lookup
            from meshcore_helpers import compute_channel_id
            channel_id = compute_channel_id(canonical_name, channel_info.get("channel_key") or "")
            found = None
            for idx, chan in self._channels_by_idx.items():
                if chan.get("channel_id") == channel_id:
                    found = idx
                    break
            if found is not None:
                channel_index = found
            else:
                # Fallback: search by name and key, canonical only
                for idx, chan in self._channels_by_idx.items():
                    if chan.get("channel_name") == canonical_name and chan.get("channel_key") == channel_info.get("channel_key"):
                        channel_index = idx
                        break
        if channel_index is None:
            self.logger.error(
                "Could not find MeshCore slot index for channel %s (id=%s). Message not sent.",
                canonical_name,
                channel_info.get("channel_id"),
            )
            return None
        try:
            self.logger.debug(
                "Sending Matrix→MeshCore (slot %s, channel: %s, user: %s): %r",
                channel_index, canonical_name, display_name, outgoing[:80],
            )
            result = await send_channel_message_with_timestamp(mc, channel_index, outgoing)
        except Exception as exc:
            # Always log on error
            self.logger.error(
                "Failed to send to MeshCore channel %s (slot %s): %s (original message: %r)",
                canonical_name,
                channel_index,
                exc,
                outgoing[:80],
            )
            return None
        # Always log at debug what happened
        if result is None:
            self.logger.debug(
                "send_channel_message_with_timestamp returned None for slot %s, channel %s (user: %s, msg: %r)",
                channel_index, canonical_name, display_name, outgoing[:80],
            )
        elif getattr(result, "type", None) == EventType.ERROR:
            self.logger.error("MeshCore rejected channel message: %s", getattr(result, "payload", None))
        else:
            self.logger.info(
                "Matrix→MeshCore [%s|slot %s] %s: %s",
                canonical_name,
                channel_index,
                display_name,
                str(getattr(result,'message', ''))[:80],
            )
        return result


    async def handle_meshtastic_message(
        self,
        packet: dict,
        formatted_message: str,
        longname: str,
        meshnet_name: str,
    ) -> bool:
        # This plugin does not handle Meshtastic messages.
        return False

    async def handle_room_message(
        self,
        room: Any,
        event: Any,
        full_message: str,
    ) -> bool:
        """Claim messages from rooms managed by this plugin.

        Returning True prevents mmrelay from relaying the message to Meshtastic.
        The actual MeshCore relay is handled by _on_matrix_room_message, which is
        registered directly on the nio client and fires for all joined rooms
        (including those not listed in mmrelay's matrix_rooms config).
        """
        channel_info = self._get_channel_info_for_room(room.room_id)
        is_dm_room = room.room_id == self._dm_room()
        return channel_info is not None or is_dm_room

    # ── Utility ───────────────────────────────────────────────────────────────

    async def _resolve_matrix_reply_target(
        self, room_id: str, event: Any
    ) -> str | None:
        """
        If *event* is a Matrix reply, fetch the original event and return the
        MeshCore node name from its body (pattern 'NodeName: message').

        MeshCore clients prepend 'NodeName: ' to all channel messages, so the
        original event body in Matrix will be formatted as:
            [mesh]: NodeName: original text
        or just:
            NodeName: original text

        Returns None if the event is not a reply, if the original event cannot
        be fetched, or if no 'NodeName: ' pattern is found.
        """
        try:
            relates_to = event.source.get("content", {}).get("m.relates_to", {})
            reply_to_id = relates_to.get("m.in_reply_to", {}).get("event_id")
        except Exception:
            return None

        if not reply_to_id:
            return None

        try:
            from mmrelay.matrix_utils import matrix_client  # type: ignore[import-untyped]
        except ImportError:
            return None

        if matrix_client is None:
            return None

        try:
            from nio import RoomGetEventResponse  # type: ignore[import-untyped]

            resp = await matrix_client.room_get_event(room_id, reply_to_id)
            if not isinstance(resp, RoomGetEventResponse):
                return None
            original_body = getattr(resp.event, "body", "") or ""
        except Exception as exc:
            self.logger.debug("Could not fetch reply target event: %s", exc)
            return None

        # Strip any prefix brackets like "[mesh]: " before looking for "NodeName: "
        text = original_body.strip()
        if text.startswith("[") and "]: " in text:
            text = text[text.index("]: ") + 3:]

        if ": " not in text:
            return None

        candidate = text.split(": ", 1)[0].strip()
        if candidate and len(candidate) <= 30 and not candidate.startswith(("@", "!", "[")):
            return candidate

        return None


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

Community plugin for meshtastic-matrix-relay (mmrelay) that bridges Matrix rooms
with MeshCore radio network channels, analogous to how mmrelay bridges Meshtastic.

Relay directions:
  - MeshCore → Matrix: channel messages and direct messages forwarded to Matrix rooms.
  - Matrix → MeshCore: messages from mapped Matrix rooms sent to MeshCore channels.

Requires mmrelay >= 1.4 and meshcore >= 2.3.7.
"""

from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import time
from collections import OrderedDict
from typing import Any

try:
    from nio import MatrixRoom, RoomGetEventResponse, RoomMessageText
except ImportError:
    pass  # resolved at runtime inside mmrelay's environment

try:
    from mmrelay.log_utils import get_logger
    from mmrelay.plugins.base_plugin import BasePlugin
except ImportError:
    from plugins.base_plugin import BasePlugin  # type: ignore[no-redef]

# Max MeshCore radio message length (bytes).  Most firmware variants cap at ~200 bytes.
_MAX_MSG_LEN = 200
# How many {txt_hash: transport_code} entries to cache for channel sender resolution.
_HASH_CACHE_MAX = 200


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
        # Cache: txt_hash (int) → transport_code (hex str) for channel sender resolution.
        # Uses OrderedDict so we can evict oldest entries cheaply.
        self._msg_hash_cache: OrderedDict[int, str] = OrderedDict()
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
        return self.config.get("channel_mappings") or []

    def _get_matrix_room_for_channel(self, channel_idx: int) -> str | None:
        for m in self._channel_mappings():
            if m.get("meshcore_channel") == channel_idx:
                return m.get("matrix_room")
        return None

    def _get_meshcore_channel_for_room(self, room_id: str) -> int | None:
        for m in self._channel_mappings():
            if m.get("matrix_room") == room_id:
                return m.get("meshcore_channel")
        return None

    def _dm_room(self) -> str | None:
        return self.config.get("direct_message_room")

    def _mesh_name(self) -> str:
        return self.config.get("mesh_name", "MeshCore")

    def _fmt_channel_prefix(self, sender: str | None, channel_idx: int) -> str:
        if not self.config.get("channel_prefix_enabled", True):
            return ""
        fmt = self.config.get("channel_prefix_format", "[{sender}/{mesh}]: ")
        return fmt.format(
            sender=sender or "?",
            mesh=self._mesh_name(),
            channel=channel_idx,
        )

    def _fmt_dm_prefix(self, sender: str | None, pubkey_short: str) -> str:
        if not self.config.get("dm_prefix_enabled", True):
            return ""
        fmt = self.config.get("dm_prefix_format", "[{sender}@{mesh}]: ")
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
        try:
            from mmrelay import meshtastic_utils  # type: ignore[attr-defined]

            loop = meshtastic_utils.event_loop
            if loop is None or not loop.is_running():
                self.logger.error(
                    "asyncio event loop not available; MeshCore listener will not start"
                )
                return
            self._listener_future = asyncio.run_coroutine_threadsafe(
                self._meshcore_listener(), loop
            )
            self.logger.info("MeshCore listener task scheduled on event loop")
        except Exception as exc:
            self.logger.error("Failed to start MeshCore listener: %s", exc)

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

        self.logger.info("MeshCore: %s ↔ Matrix  (mesh: %s)", self._mesh_name(), self._mesh_name())
        self.logger.info("  Connection : %s  →  %s", conn_type.upper(), target)
        self.logger.info(
            "  Reconnect  : %s (max %s attempts)",
            "✅ enabled" if conn_cfg.get("auto_reconnect", True) else "❌ disabled",
            conn_cfg.get("max_reconnect_attempts", 5),
        )

        if mappings:
            self.logger.info("  MeshCore Channels ↔ Matrix Rooms (%d configured):", len(mappings))
            for m in mappings:
                ch = m.get("meshcore_channel", "?")
                room = m.get("matrix_room", "?")
                self.logger.info("    Channel %s  →  %s", ch, room)
        else:
            self.logger.warning("  ⚠️  No channel_mappings configured — relay inactive")

        if dm_room:
            self.logger.info("  DM room    : %s  (receive-only)", dm_room)

        ch_fmt = self.config.get("channel_prefix_format", "[{sender}/{mesh}]: ")
        dm_fmt = self.config.get("dm_prefix_format", "[{sender}@{mesh}]: ")
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

    # ── MeshCore background listener ──────────────────────────────────────────

    async def _meshcore_listener(self) -> None:
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

        while not self._stop_event.is_set():
            mc = None
            try:
                self.logger.info("Connecting to MeshCore device (%s → %s)…", conn_type.upper(), target)
                mc = await self._connect_meshcore(conn_cfg)
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
                mc.subscribe(EventType.RX_LOG_DATA, self._on_rx_log_data)
                mc.subscribe(EventType.CHANNEL_MSG_RECV, self._on_channel_msg)
                mc.subscribe(EventType.CONTACT_MSG_RECV, self._on_contact_msg)

                # Load channel secrets into the parser so RX_LOG_DATA can be
                # decrypted and correlated with CHANNEL_MSG_RECV for sender resolution.
                if self._channel_mappings():
                    mc.set_decrypt_channel_logs(True)
                    for mapping in self._channel_mappings():
                        idx = mapping.get("meshcore_channel")
                        if idx is not None:
                            self.logger.debug("Fetching channel info for channel %d", idx)
                            await mc.commands.device.get_channel(idx)

                # Populate contacts on startup.
                await mc.ensure_contacts()
                # Handle queued messages and keep fetching new ones automatically.
                await mc.start_auto_message_fetching()

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
        auto_reconnect: bool = conn_cfg.get("auto_reconnect", True)
        max_attempts: int = conn_cfg.get("max_reconnect_attempts", 5)

        try:
            if conn_type == "tcp":
                return await MeshCore.create_tcp(
                    host=conn_cfg.get("host", "localhost"),
                    port=conn_cfg.get("port", 5000),
                    auto_reconnect=auto_reconnect,
                    max_reconnect_attempts=max_attempts,
                )
            if conn_type == "serial":
                return await MeshCore.create_serial(
                    port=conn_cfg.get("serial_port", "/dev/ttyUSB0"),
                    auto_reconnect=auto_reconnect,
                    max_reconnect_attempts=max_attempts,
                )
            if conn_type == "ble":
                return await MeshCore.create_ble(
                    address=conn_cfg.get("ble_address"),
                    auto_reconnect=auto_reconnect,
                    max_reconnect_attempts=max_attempts,
                )
            self.logger.error("Unknown MeshCore connection type: %s", conn_type)
            return None
        except Exception as exc:
            self.logger.error("MeshCore connection failed: %s", exc)
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

    async def _on_rx_log_data(self, event: Any) -> None:
        """
        Cache the transport_code ↔ msg_hash association for channel sender resolution.

        For TC_FLOOD/TC_DIRECT packets the firmware includes a 4-byte sender
        transport code (first 4 bytes of sender's public key).  The meshcore_parser
        computes msg_hash = SHA256(sender_timestamp_le4 + plaintext)[0:4] and stores
        it in log_data when decrypt_channels=True.  We mirror this cache so that
        when CHANNEL_MSG_RECV arrives we can look up the sender.
        """
        log_data = event.payload
        transport_code: str | None = log_data.get("transport_code")
        msg_hash: int | None = log_data.get("msg_hash")
        if transport_code and msg_hash is not None:
            self._msg_hash_cache[msg_hash] = transport_code
            # Evict oldest entries to keep memory bounded.
            while len(self._msg_hash_cache) > _HASH_CACHE_MAX:
                self._msg_hash_cache.popitem(last=False)

    async def _on_channel_msg(self, event: Any) -> None:
        msg = event.payload
        channel_idx: int = msg.get("channel_idx", -1)
        text: str = (msg.get("text") or "").strip()

        if not text:
            return

        matrix_room = self._get_matrix_room_for_channel(channel_idx)
        if not matrix_room:
            self.logger.debug("Channel %d message dropped (no Matrix room mapped)", channel_idx)
            return

        sender_name = self._resolve_channel_sender(msg)
        prefix = self._fmt_channel_prefix(sender_name, channel_idx)
        full_msg = prefix + text

        self.logger.info(
            "MeshCore→Matrix [ch%d] %s: %s", channel_idx, sender_name or "?", text[:80]
        )
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

    # ── Sender resolution ─────────────────────────────────────────────────────

    def _resolve_channel_sender(self, msg: dict) -> str | None:
        """
        Attempt to resolve the display name of the sender of a channel message.

        Channel messages do not carry sender identity directly.  For TC_FLOOD /
        TC_DIRECT packets the radio log (RX_LOG_DATA) contains a 4-byte transport
        code (first 4 bytes of sender's pubkey).  We correlate via:
            txt_hash = SHA256(sender_timestamp_le4 + text_bytes)[0:4]  (little-endian int)
        which matches the msg_hash computed by meshcore_parser when decrypt_channels=True.

        Returns None when the sender cannot be identified (FLOOD packets or cache miss).
        """
        sender_timestamp: int = msg.get("sender_timestamp", 0)
        text: str = msg.get("text") or ""

        ts_bytes = sender_timestamp.to_bytes(4, "little", signed=False)
        text_bytes = text.encode("utf-8", errors="replace")
        txt_hash = int.from_bytes(
            hashlib.sha256(ts_bytes + text_bytes).digest()[:4], "little", signed=False
        )

        transport_code = self._msg_hash_cache.get(txt_hash)
        if not transport_code:
            return None

        return self._lookup_name_by_prefix(transport_code)

    # ── Matrix → MeshCore ─────────────────────────────────────────────────────

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
        """
        Relay a Matrix room message to the corresponding MeshCore channel.

        Only handles messages from rooms explicitly mapped in channel_mappings.
        Direct-message rooms are receive-only; Matrix messages sent there are claimed
        (returned True) but not forwarded, so the normal mmrelay Meshtastic path does
        not pick them up.
        Returns True to claim the message (preventing other plugins from double-relaying),
        False when the room is not managed by this plugin at all.
        """
        channel_idx = self._get_meshcore_channel_for_room(room.room_id)
        is_dm_room = room.room_id == self._dm_room()

        if channel_idx is None and not is_dm_room:
            return False

        # DM room is receive-only — claim the message but do not forward it.
        if is_dm_room:
            return True

        # For channel rooms, only relay plain text messages; claim all other
        # event types so they don't leak to the default Meshtastic relay path.
        try:
            from nio import RoomMessageText  # type: ignore[import-untyped]

            if not isinstance(event, RoomMessageText):
                return True  # Claim but don't relay non-text events.
        except ImportError:
            pass

        mc = self._mc
        if mc is None or not mc.is_connected:
            self.logger.warning(
                "MeshCore not connected; dropping Matrix message from %s", room.room_id
            )
            return True  # Claim it so the regular mesh_relay_plugin doesn't try to send it.

        # Resolve sender display name (fall back to local part of Matrix ID).
        try:
            display_name = room.user_name(event.sender) or event.sender
        except Exception:
            display_name = event.sender
        if not display_name:
            display_name = event.sender

        # Build the text to send.  Matrix clients include a fallback quote block
        # in the body when the user sends a threaded reply ("> sender  original\n\nreply"),
        # so full_message already has the reply context — no extra processing needed.
        outgoing = self._truncate(self._fmt_matrix_prefix(display_name) + full_message)

        try:
            result = await mc.commands.send_chan_msg(channel_idx, outgoing)
            from meshcore.events import EventType  # type: ignore[import-untyped]

            if result is not None and result.type == EventType.ERROR:
                self.logger.error(
                    "MeshCore rejected channel message: %s", result.payload
                )
            else:
                self.logger.info(
                    "Matrix→MeshCore [ch%d] %s: %s",
                    channel_idx,
                    display_name,
                    outgoing[:80],
                )
        except Exception as exc:
            self.logger.error("Failed to send to MeshCore channel %d: %s", channel_idx, exc)

        return True

    # ── Utility ───────────────────────────────────────────────────────────────

    def _truncate(self, text: str) -> str:
        if len(text.encode("utf-8")) > _MAX_MSG_LEN:
            # Truncate conservatively on character boundary.
            while len(text.encode("utf-8")) > _MAX_MSG_LEN - 3:
                text = text[:-1]
            text += "…"
        return text

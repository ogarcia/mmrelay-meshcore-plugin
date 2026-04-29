# mmrelay-meshcore-plugin

Community plugin for [meshtastic-matrix-relay (mmrelay)](https://github.com/geoffwhittington/meshtastic-matrix-relay) that bridges **MeshCore** radio networks with **Matrix** chat rooms — analogous to how mmrelay bridges Meshtastic with Matrix.

## Features

- **MeshCore → Matrix**: channel messages and direct messages forwarded to configured Matrix rooms.
- **Matrix → MeshCore**: messages in mapped Matrix rooms sent to the corresponding MeshCore channel.
- **Room mapping precedence**: If a Matrix room is configured both in this plugin and in mmrelay's `matrix_rooms`, the plugin will claim incoming Matrix messages for that room and relay them only to MeshCore (not to Meshtastic). This prevents duplicate relays or message loops. Rooms configured only in the plugin work normally in both directions.
- **Sender identification**: for direct messages, the sender's name is resolved from a local contacts database. For channel messages, MeshCore clients already include the sender name in the message text.
- **Contacts persistence**: known MeshCore nodes are stored in mmrelay's SQLite database and used to resolve names for direct messages.
- **Prefix formatting**: flexible format strings with named variables (`{sender}`, `{mesh}`, `{channel}`, `{display}`, `{pubkey}`).
- **Reply handling**: Matrix replies are converted to MeshCore `@[NodeName]` mentions when the original sender can be identified; the quote block is stripped to preserve the 200-byte message limit.

## Requirements

- mmrelay ≥ 1.4
- Python ≥ 3.10
- `meshcore` ≥ 2.3.7 (auto-installed by mmrelay when `install_requirements: true`)

## Configuration

Add a section under `community-plugins` in your mmrelay `config.yaml`:

```yaml
community-plugins:
  meshcore-matrix-relay:
    active: true
    repository: https://github.com/ogarcia/mmrelay-meshcore-plugin.git
    branch: master
    install_requirements: true

    # ── MeshCore device connection ──────────────────────────────────────────
    connection:
      type: tcp                    # tcp | serial | ble
      host: 192.168.1.50           # TCP only
      port: 5000                   # TCP only (default: 5000)
      #serial_port: /dev/ttyUSB0   # serial only
      #ble_address: AA:BB:CC:DD:EE:FF  # BLE only

    # ── Optional: friendly name shown in prefix variables {mesh} ───────────
    mesh_name: MeshCore

    # ── Prefix for MeshCore channel messages relayed to Matrix ─────────────
    # MeshCore clients already prepend the sender name to the message text,
    # so this prefix is disabled by default. Variables: {mesh}, {channel}
    channel_prefix_enabled: false
    channel_prefix_format: "[{mesh}]: "

    # ── Prefix for MeshCore direct messages relayed to Matrix ──────────────
    # Variables: {sender} (adv_name from DB, falls back to pubkey), {pubkey} (first 8 hex chars), {mesh}
    dm_prefix_enabled: true
    dm_prefix_format: "[DM] {sender}({pubkey}): "

    # ── Prefix prepended to Matrix messages sent to MeshCore ───────────────
    # Variables: {display} (Matrix display name)
    matrix_prefix_enabled: true
    matrix_prefix_format: "{display}[M]: "

    # ── Channel mappings: MeshCore ↔ Matrix ──────────────────────────────
    # Only named channels with PSK are supported.
    # Provide the channel name and its PSK key (hex string, 32 chars).
    # The plugin auto-discovers channels from CHANNEL_INFO events sent by the node.
    # If CHANNEL_INFO is not available, the channel name is inferred from the
    # message content (MeshCore clients prepend "ChannelName: " to messages).
    channel_mappings:
      # Default "Public" channel (key is SHA256("Public")[0:16])
      - matrix_room: "!someroomid:example.matrix.org"
        meshcore_channel_name: "Public"
        meshcore_channel_key: "8B3387E9C5CDEA6AC9E5EDBAA115CD72"
      # Named channel with custom key
      - matrix_room: "!otherroomid:example.matrix.org"
        meshcore_channel_name: "GALICIA"
        meshcore_channel_key: "F32E1D081E0FE4C4849BE4324BE2CBD9"

    # ── Optional: Matrix room for incoming MeshCore direct messages ─────────
    # This room is receive-only; messages sent in it are not forwarded.
    #direct_message_room: "!dmroomid:example.matrix.org"
```

### Connection types

| Type | Required fields |
|------|----------------|
| `tcp` | `host`, `port` (default 5000) |
| `serial` | `serial_port` (e.g. `/dev/ttyUSB0`) |
| `ble` | `ble_address` (MAC address) |

### Matrix room configuration

The plugin manages its Matrix rooms independently of mmrelay's `matrix_rooms` setting. At startup, it joins every room listed in `channel_mappings` (and `direct_message_room` if set) and registers its own Matrix event handler on the Matrix client.

If a room is configured only in this plugin, it works as expected: MeshCore→Matrix and Matrix→MeshCore both function normally.

If a Matrix room is configured in both this plugin and in mmrelay's `matrix_rooms`, the plugin will claim incoming Matrix messages for that room and relay them only to MeshCore (not to Meshtastic). This prevents duplicate relays or message loops. The behavior is the same regardless of whether `meshtastic_channel` is set for that room in `matrix_rooms`.

## Prefix format variables

| Variable | Available in | Value |
|----------|--------------|-------|
| `{sender}` | DM → Matrix | `adv_name` from contacts DB; falls back to `{pubkey}` if unresolvable |
| `{pubkey}` | DM → Matrix | First 8 hex characters of the sender's public key |
| `{mesh}` | channel → Matrix (if enabled), DM → Matrix | Value of `mesh_name` in config |
| `{channel}` | channel → Matrix | MeshCore channel name (the `meshcore_channel_name` from config) |
| `{display}` | Matrix → MeshCore | Matrix display name of the sender |

## How sender identification works

MeshCore channel messages do not carry sender identity by design — the MeshCore client application prepends the sender name directly to the message text (e.g. `EA1ABC: hello`).  For this reason `channel_prefix_enabled` defaults to `false`.

Direct messages always carry a 6-byte pubkey prefix which is looked up directly in the contacts database.  If the sender is not found, the pubkey prefix is used as a fallback.

## Contacts database

The plugin creates a `meshcore_contacts` table in mmrelay's existing SQLite database.  It is populated:

- On startup (`ensure_contacts`)
- When a new node advertises itself (`ADVERTISEMENT` / `NEW_CONTACT` events)

The table schema:

```sql
CREATE TABLE IF NOT EXISTS meshcore_contacts (
    pubkey_prefix  TEXT PRIMARY KEY,  -- full 32-byte hex public key
    adv_name       TEXT,
    last_advert    INTEGER,
    lat            REAL,
    lon            REAL,
    last_seen      INTEGER
);
```

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE) for details.

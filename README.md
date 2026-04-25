# mmrelay-meshcore-plugin

Community plugin for [meshtastic-matrix-relay (mmrelay)](https://github.com/geoffwhittington/meshtastic-matrix-relay) that bridges **MeshCore** radio networks with **Matrix** chat rooms — analogous to how mmrelay bridges Meshtastic with Matrix.

## Features

- **MeshCore → Matrix**: channel messages and direct messages forwarded to configured Matrix rooms.
- **Matrix → MeshCore**: messages in mapped Matrix rooms sent to the corresponding MeshCore channel.
- **Sender identification**: for TC_FLOOD packets, the sender's name is resolved from a local contacts database and shown in the prefix.
- **Contacts persistence**: known MeshCore nodes are stored in mmrelay's SQLite database and used to resolve names even when the node is not currently active.
- **Prefix formatting**: flexible format strings with named variables (`{sender}`, `{mesh}`, `{channel}`, `{display}`, `{pubkey}`).
- **Reply passthrough**: Matrix reply fallback text (`> quote\n\nreply`) is forwarded to MeshCore as-is.

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
      auto_reconnect: true
      max_reconnect_attempts: 5

    # ── Optional: friendly name shown in prefix variables {mesh} ───────────
    mesh_name: MeshCore

    # ── Prefix for MeshCore channel messages relayed to Matrix ─────────────
    # Variables: {sender} (adv_name of the node, or "?" if unresolvable), {mesh}, {channel}
    channel_prefix_enabled: true
    channel_prefix_format: "[{sender}/{mesh}]: "

    # ── Prefix for MeshCore direct messages relayed to Matrix ──────────────
    # Variables: {sender} (adv_name of the node), {pubkey} (first 8 hex chars of pubkey), {mesh}
    dm_prefix_enabled: true
    dm_prefix_format: "[{sender}@{mesh}]: "

    # ── Prefix prepended to Matrix messages sent to MeshCore ───────────────
    # Variables: {display} (Matrix display name)
    matrix_prefix_enabled: true
    matrix_prefix_format: "{display}[M]: "

    # ── Channel mappings: MeshCore channel index ↔ Matrix room ─────────────
    channel_mappings:
      - matrix_room: "!someroomid:example.matrix.org"
        meshcore_channel: 0
      - matrix_room: "!otherroomid:example.matrix.org"
        meshcore_channel: 1

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

## Prefix format variables

| Variable | Available in | Value |
|----------|--------------|-------|
| `{sender}` | channel & DM → Matrix | `adv_name` of the node from the contacts DB; `?` if unresolvable |
| `{pubkey}` | DM → Matrix | First 8 hex characters of the sender's public key |
| `{mesh}` | channel & DM → Matrix | Value of `mesh_name` in config |
| `{channel}` | channel → Matrix | Numeric MeshCore channel index |
| `{display}` | Matrix → MeshCore | Matrix display name of the sender |

## How sender identification works

MeshCore channel messages do not carry sender identity by design.  For **TC_FLOOD** and **TC_DIRECT** packets the firmware includes a 4-byte *transport code* (first 4 bytes of the sender's public key) in the raw radio log.  The plugin correlates the log entry with the delivered message via `SHA256(sender_timestamp_le4 || plaintext)[0:4]`, then looks up the sender in the contacts database.

For **FLOOD** packets (no transport code) the sender appears as `?` in the prefix.

Direct messages always carry a 6-byte pubkey prefix which is looked up directly.

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

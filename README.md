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
     #
     # NEW: You may (and should, for exact matching) provide the slot index of
     # the channel on the MeshCore node using the optional meshcore_channel_index field.
     # This is the real channel slot used internally by MeshCore for sending.
     #
     # If meshcore_channel_index is omitted, the plugin will autodetect the correct slot
     # by matching name/key with active slots on the MeshCore node at runtime.
     # DO NOT assume the slot matches your config order; check with your node UI or status.
     # Messages will ONLY be sent if an exact match is found. See troubleshooting.
     channel_mappings:
       # Default public hashtag channel (channel_id is SHA256("#Public")[0:16], key not required)
       - matrix_room: "!someroomid:example.matrix.org"
         meshcore_channel_name: "#Public"
         # meshcore_channel_index: 0  # Optional: slot index for hashtag/public channel
       # Named channel with custom key (explicit slot index)
       - matrix_room: "!otherroomid:example.matrix.org"
         meshcore_channel_name: "GALICIA"
         meshcore_channel_key: "F32E1D081E0FE4C4849BE4324BE2CBD9"
         meshcore_channel_index: 5   # <=== Slot index assigned on your MeshCore node

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

## How it works: Channel slot index (slot/idx)

MeshCore requires all messages to target the correct slot (channel index) as configured on the node itself.

- You MUST specify the correct slot/slot index in `meshcore_channel_index` for fully deterministic sending, especially if you change channel order or add channels on the node.
- If omitted, the plugin will try to autodetect the slot/index by matching name/key to active slots. Messages are only sent if the slot is found, otherwise a clear error is logged and the message is NOT sent.
- This design avoids the classic ERR_CODE_NOT_FOUND problem when `channel_id`/hash does not match the internal slot index on the node.
- Double check your MeshCore node UI or remote terminal to find the slot index/position, and match it in your mapping.

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

## Development & Testing

This plugin is designed to be easily testable and maintainable. All logic related to MeshCore messaging is decoupled into helpers, allowing unit tests to run without requiring the full mmrelay/Matrix runtime environment.

### Development requirements

- Python ≥ 3.10
- Set up a virtual environment:
  ```bash
  python -m venv .venv
  source .venv/bin/activate
  pip install -r requirements.txt
  pip install pytest pytest-asyncio
  ```

### Running unit tests

All tests are located in the `.tests/` directory (note the leading dot).
This ensures they are never loaded by the plugin system at runtime.

To run unit tests:
```
source .venv/bin/activate
pytest .tests
```

#### Message size and content limits

- MeshCore channel messages have a maximum size of 200 bytes. Longer messages will be truncated by the plugin.
- Only printable UTF-8 characters are allowed. Control characters, tabs, and certain unicode zero-width characters are stripped during message processing.
- Always sanitize content before sending (the plugin does this automatically).


#### About the MeshCore helper

The helper function for sending messages to MeshCore channels is decoupled in `meshcore_send_helper.py`. This pure function adds the timestamp and performs the send, and can be unit tested directly using mocks for the `mc` object and its methods.

Note: the helper **does not sanitize** the message; always sanitize display names and message bodies before sending (the plugin applies this by default).

For an example, see the real test at `.tests/test_meshcore_send_helper.py`.

**Important:**

- Never place test files or extra code directly in the plugin directory. Use a directory such as `.tests/` to avoid accidental loading by the plugin host (mmrelay and similar systems will try to import all top-level .py files and subdirectories).
- This structure allows you to include as many dev/test files as needed without risk of breaking plugin deployment or requiring test dependencies like `pytest` in production.

---

## License

GNU General Public License v3.0 — see [LICENSE](LICENSE) for details.

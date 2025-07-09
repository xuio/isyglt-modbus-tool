# ISYGLT Merker Monitor

A minimal Python tool that emulates an ISYGLT IP-Master via Modbus-TCP and provides a live browser UI to observe and manipulate the Merker memory (256 bytes).

## Features
* Modbus-TCP server exposing:
  * 2048 coils → `NE1.1` … `NE256.8` (1-based bit numbering)
  * 256 holding registers → `NE1` … `NE256`
* Integrated HTTP (port 8000) & WebSocket (port 8001) UI with:
  * Real-time table, colour-coded bits, keyboard/mouse control
  * Per-byte decimal/hex edit and persistent labels
* State & label persistence (`state.json`, `labels.json`)

## Quick start
```bash
# create / activate virtual env (optional)
uv venv
. .venv/bin/activate

# install deps (pymodbus & websockets) exactly as locked
uv pip sync

# run server (tries 502, falls back to 1502)
python dummy_isyglt_modbus.py
```
Then open <http://localhost:8000> in your browser.

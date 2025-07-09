#!/usr/bin/env python3
"""ISYGLT dummy Modbus/TCP server

Emulates the Modbus behaviour of an IP-Master with a single NE marker bank.

Features
---------
* 256 NE bytes (NE0 .. NE255) stored internally
* Coils 0-2047 map to the single bits  NE0.1 … NE255.8  (bit0 = .1)
* Holding registers 0-127 expose the bytes in pairs:  low byte = NE[2*n],
  high byte = NE[2*n+1]
* Writes to coils or holding registers update the same NE memory, so both
  views stay consistent.
* Periodically prints the current marker state (bytes + first bits) so you
  can watch what external Modbus clients do.

Requirements
~~~~~~~~~~~~
    pip install pymodbus>=2.5

Run
~~~
    python3 dummy_isyglt_modbus.py [port]
    # Automatically uses port 502 if running with privileges (sudo/admin)
    # Otherwise defaults to port 1502 (no privileges needed)
    # You can override by specifying a port number
"""

import sys
import threading
import time
from typing import List, Any
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import asyncio
import websockets
import logging
from functools import partial
import os
from pathlib import Path

# Suppress websocket logs
logging.getLogger('websockets').setLevel(logging.WARNING)

from pymodbus.datastore import ModbusSequentialDataBlock, ModbusSlaveContext, ModbusServerContext
try:
    # Pymodbus < 3.x
    from pymodbus.server.sync import StartTcpServer  # type: ignore
except ImportError:  # pragma: no cover
    # Pymodbus >= 3.x (API reorganised)
    from pymodbus.server import StartTcpServer
from pymodbus.device import ModbusDeviceIdentification

# ---------------------------------------------------------------------------
# Data blocks mapping NE memory to coils / holding registers
# ---------------------------------------------------------------------------

NE_SIZE = 256  # bytes
ne_lock = threading.Lock()
COIL_COUNT = NE_SIZE * 8  # 2048
REG_COUNT = NE_SIZE // 2  # 128

# Track WebSocket clients
ws_clients: set[Any] = set()  # WebSocket connections
ws_lock = threading.Lock()
update_queue: asyncio.Queue[List[int]] = None  # Will be initialized later
ws_loop: asyncio.AbstractEventLoop = None  # WebSocket event loop

# Labels storage
LABELS_FILE = "labels.json"
ne_labels: dict[int, str] = {}
labels_lock = threading.Lock()

# Merker state storage
STATE_FILE = "state.json"
state_lock = threading.Lock()

BIT_LABELS_FILE = "bit_labels.json"
bit_labels: dict[str, str] = {}
bit_labels_lock = threading.Lock()

def _bit_key(byte: int, bit: int) -> str:
    return f"{byte}_{bit}"

def load_bit_labels():
    global bit_labels
    if os.path.exists(BIT_LABELS_FILE):
        try:
            with open(BIT_LABELS_FILE, 'r') as f:
                bit_labels = json.load(f)
            print(f"Loaded {len(bit_labels)} bit labels from {BIT_LABELS_FILE}")
        except Exception as e:
            print(f"Error loading bit labels: {e}")
            bit_labels = {}
    else:
        bit_labels = {}

def save_bit_labels():
    try:
        with bit_labels_lock:
            with open(BIT_LABELS_FILE, 'w') as f:
                json.dump(bit_labels, f, indent=2)
    except Exception as e:
        print(f"Error saving bit labels: {e}")

async def broadcast_bitlabels():
    with bit_labels_lock:
        data = dict(bit_labels)
    message = json.dumps({"type": "bitlabels", "data": data})
    with ws_lock:
        dead=set()
        for c in ws_clients:
            try:
                await c.send(message)
            except:
                dead.add(c)
        ws_clients.difference_update(dead)


def load_labels():
    """Load labels from persistent storage."""
    global ne_labels
    if os.path.exists(LABELS_FILE):
        try:
            with open(LABELS_FILE, 'r') as f:
                ne_labels = {int(k): v for k, v in json.load(f).items()}
            print(f"Loaded {len(ne_labels)} labels from {LABELS_FILE}")
        except Exception as e:
            print(f"Error loading labels: {e}")
            ne_labels = {}
    else:
        ne_labels = {}


def save_labels():
    """Save labels to persistent storage."""
    try:
        with labels_lock:
            with open(LABELS_FILE, 'w') as f:
                json.dump(ne_labels, f, indent=2)
    except Exception as e:
        print(f"Error saving labels: {e}")


def load_state(ne_memory: List[int]):
    """Load merker state from persistent storage."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, 'r') as f:
                data = json.load(f)
                # Restore non-zero values
                for idx_str, value in data.items():
                    idx = int(idx_str)
                    if 0 <= idx < NE_SIZE:
                        ne_memory[idx] = value & 0xFF
            non_zero = sum(1 for v in ne_memory if v != 0)
            print(f"Loaded merker state from {STATE_FILE} ({non_zero} active)")
        except Exception as e:
            print(f"Error loading state: {e}")


def save_state(ne_memory: List[int]):
    """Save merker state to persistent storage."""
    try:
        with state_lock:
            # Only save non-zero values to keep file small
            non_zero_state = {str(i): v for i, v in enumerate(ne_memory) if v != 0}
            with open(STATE_FILE, 'w') as f:
                json.dump(non_zero_state, f, indent=2)
    except Exception as e:
        print(f"Error saving state: {e}")


def _dump_state(ne: List[int]):
    """Return a formatted string of the first few bytes/bits."""
    # Find non-zero bytes for summary
    non_zero = [(i, v) for i, v in enumerate(ne) if v != 0]
    
    if not non_zero:
        return "All merkers are zero"
    
    # Show first few non-zero values
    summary = f"Active: {len(non_zero)}/{NE_SIZE} | "
    
    # Show up to 5 non-zero values
    for i, (idx, val) in enumerate(non_zero[:5]):
        if i > 0:
            summary += ", "
        # Show with label if available
        label = ""
        with labels_lock:
            if idx in ne_labels:
                label = f" ({ne_labels[idx][:20]})"
        summary += f"NE{idx}=0x{val:02X}{label}"
    
    if len(non_zero) > 5:
        summary += f", ... +{len(non_zero)-5} more"
    
    return summary


async def broadcast_state(data: List[int]):
    """Send current state to all connected WebSocket clients."""
    message = json.dumps({"type": "state", "data": data})
    
    with ws_lock:
        dead_clients = set()
        for client in ws_clients:
            try:
                await client.send(message)
            except:
                dead_clients.add(client)
        ws_clients.difference_update(dead_clients)


async def broadcast_labels():
    """Send current labels to all connected WebSocket clients."""
    with labels_lock:
        labels_data = dict(ne_labels)
    message = json.dumps({"type": "labels", "data": labels_data})
    
    with ws_lock:
        dead_clients = set()
        for client in ws_clients:
            try:
                await client.send(message)
            except:
                dead_clients.add(client)
        ws_clients.difference_update(dead_clients)


class NECoilBlock(ModbusSequentialDataBlock):
    """2048 coils backed by the NE byte array."""

    def __init__(self, ne_mem: List[int]):
        # Don't call parent __init__ to avoid creating separate storage
        self.address = 0
        self.ne = ne_mem
        self.default_value = 0
        print(f"[DEBUG] NECoilBlock.__init__: ne_mem id = {id(ne_mem)}, len = {len(ne_mem)}")

    def validate(self, address, count=1):
        """Check to see if the request is in range"""
        return True  # We handle bounds checking in getValues/setValues

    def getValues(self, address, count=1):  # type: ignore[override]
        print(f"[DEBUG] NECoilBlock.getValues called: address={address}, count={count}")
        with ne_lock:
            # ISY-GLT uses 1-based bit numbering (.1 to .8)
            # Modbus coil 0 = NE0.1, coil 7 = NE0.8, coil 8 = NE1.1, etc.
            values = []
            for addr in range(address, address + count):
                # ISYGLT address 0 → NE1 → shift by +1 byte
                byte_idx = addr // 8 + 1
                # Modbus addresses are effectively 1-based; adjust so coil 0 → NE0 bit 0 (.1)
                bit_idx = (addr - 1) % 8
                if byte_idx < len(self.ne):
                    # Extract bit from byte (bit 0 = .1, bit 7 = .8)
                    values.append((self.ne[byte_idx] >> bit_idx) & 1)
                else:
                    values.append(0)
            print(f"[DEBUG] NECoilBlock.getValues returning: {values}")
            return values

    def setValues(self, address, values):  # type: ignore[override]
        print(f"[DEBUG] NECoilBlock.setValues called: address={address}, values={values}")
        with ne_lock:
            for offset, val in enumerate(values):
                addr = address + offset
                # ISYGLT address 0 → NE1 → shift by +1 byte
                byte_idx = addr // 8 + 1
                # Modbus addresses are effectively 1-based; adjust so coil 0 → NE0 bit 0 (.1)
                bit_idx = (addr - 1) % 8
                if byte_idx < len(self.ne):
                    if val:
                        self.ne[byte_idx] |= 1 << bit_idx
                    else:
                        self.ne[byte_idx] &= ~(1 << bit_idx)
                    print(f"[DEBUG] Set NE{byte_idx} bit {bit_idx} to {val}, NE{byte_idx} now = 0x{self.ne[byte_idx]:02X}")
        # Queue update for WebSocket broadcast
        if update_queue:
            try:
                update_queue.put_nowait(list(self.ne))
            except:
                pass

    async def async_getValues(self, address, count=1):
        """Async version of getValues"""
        return self.getValues(address, count)
    
    async def async_setValues(self, address, values):
        """Async version of setValues"""
        return self.setValues(address, values)

    def __getattr__(self, name):
        """Catch any method calls we might be missing"""
        print(f"[DEBUG] NECoilBlock.__getattr__ called for: {name}")
        raise AttributeError(f"NECoilBlock has no attribute {name}")


class NERegisterBlock(ModbusSequentialDataBlock):
    """128 holding registers backed by the NE byte array."""

    def __init__(self, ne_mem: List[int]):
        # Don't call parent __init__ to avoid creating separate storage
        self.address = 0
        self.ne = ne_mem
        self.default_value = 0
        print(f"[DEBUG] NERegisterBlock.__init__: ne_mem id = {id(ne_mem)}, len = {len(ne_mem)}")

    def validate(self, address, count=1):
        """Check to see if the request is in range"""
        return True  # We handle bounds checking in getValues/setValues

    def getValues(self, address, count=1):  # type: ignore[override]
        print(f"[DEBUG] NERegisterBlock.getValues called: address={address}, count={count}")
        with ne_lock:
            regs = []
            for n in range(address, address + count):
                byte_idx = n  # Register n ↔ NE byte n (low)
                if byte_idx < len(self.ne):
                    low = self.ne[byte_idx]
                    high = self.ne[byte_idx + 1] if (byte_idx + 1) < len(self.ne) else 0
                    regs.append((high << 8) | low)
                else:
                    regs.append(0)
            print(f"[DEBUG] NERegisterBlock.getValues returning: {[f'0x{r:04X}' for r in regs]}")
            return regs

    def setValues(self, address, values):  # type: ignore[override]
        print(f"[DEBUG] NERegisterBlock.setValues called: address={address}, values={values}")
        with ne_lock:
            for offset, reg in enumerate(values):
                n = address + offset
                byte_idx = n  # Register n ↔ NE byte n (low)
                if byte_idx < len(self.ne):
                    low = reg & 0xFF
                    high = (reg >> 8) & 0xFF
                    # Always write low byte
                    self.ne[byte_idx] = low
                    # Write high byte only if non-zero (i.e., full 16-bit word intent)
                    if high or reg > 0xFF:
                        if (byte_idx + 1) < len(self.ne):
                            self.ne[byte_idx + 1] = high
                    print(f"[DEBUG] Write reg: NE{byte_idx}=0x{low:02X}, NE{byte_idx+1 if (byte_idx+1)<len(self.ne) else 'X'}=0x{high:02X}")
        # Queue update for WebSocket broadcast
        if update_queue:
            try:
                update_queue.put_nowait(list(self.ne))
            except:
                pass

    async def async_getValues(self, address, count=1):
        """Async version of getValues"""
        return self.getValues(address, count)
    
    async def async_setValues(self, address, values):
        """Async version of setValues"""
        return self.setValues(address, values)


# ---------------------------------------------------------------------------
# Helper: background printer
# ---------------------------------------------------------------------------

def start_monitor(ne_mem: List[int]):
    def _printer():
        import datetime
        last_save = time.time()
        while True:
            timestamp = datetime.datetime.now().strftime("%H:%M:%S")
            status = _dump_state(ne_mem)
            print(f"[{timestamp}] {status}")
            
            # Auto-save state every 30 seconds
            if time.time() - last_save > 30:
                save_state(ne_mem)
                last_save = time.time()
            
            time.sleep(10)  # Changed from 5 to 10 seconds

    t = threading.Thread(target=_printer, daemon=True)
    t.start()


async def websocket_handler(websocket: Any, ne_mem: List[int]):
    """Handle WebSocket connections."""
    client_addr = f"{websocket.remote_address[0]}:{websocket.remote_address[1]}"
    print(f"[WS] Client connected: {client_addr}")
    with ws_lock:
        ws_clients.add(websocket)
    
    try:
        # Send initial state
        with ne_lock:
            initial_data = list(ne_mem)
        await websocket.send(json.dumps({"type": "state", "data": initial_data}))
        
        # Send initial labels
        with labels_lock:
            labels_data = dict(ne_labels)
        await websocket.send(json.dumps({"type": "labels", "data": labels_data}))
        with bit_labels_lock:
            bit_labels_copy=dict(bit_labels)
        await websocket.send(json.dumps({"type": "bitlabels", "data": bit_labels_copy}))
        
        # Handle incoming messages
        async for message in websocket:
            try:
                data = json.loads(message)
                if data.get("type") == "write":
                    idx = int(data.get("index", -1))
                    val = int(data.get("value", 0)) & 0xFF
                    if 0 <= idx < NE_SIZE:
                        with ne_lock:
                            ne_mem[idx] = val
                        # Queue the update
                        if update_queue:
                            update_queue.put_nowait(list(ne_mem))
                        # Only log significant changes
                        if val != 0:
                            label = ""
                            with labels_lock:
                                if idx in ne_labels:
                                    label = f" ({ne_labels[idx]})"
                            print(f"[WS] {client_addr}: NE{idx} = 0x{val:02X}{label}")
                elif data.get("type") == "label":
                    idx = int(data.get("index", -1))
                    label = data.get("label", "").strip()
                    if 0 <= idx < NE_SIZE:
                        with labels_lock:
                            if label:
                                ne_labels[idx] = label
                                print(f"[WS] {client_addr}: Label NE{idx} = '{label}'")
                            else:
                                ne_labels.pop(idx, None)
                        save_labels()
                        # Broadcast label update to all clients
                        await broadcast_labels()
                elif data.get("type") == "bitlabel":
                    byte_idx = int(data.get("byte", -1))
                    bit_idx = int(data.get("bit", -1))
                    label = data.get("label", "").strip()
                    key=_bit_key(byte_idx, bit_idx)
                    with bit_labels_lock:
                        if label:
                            bit_labels[key]=label
                        else:
                            bit_labels.pop(key, None)
                    save_bit_labels()
                    await broadcast_bitlabels()
                elif data.get("type") == "reset":
                    with ne_lock:
                        for i in range(NE_SIZE):
                            ne_mem[i] = 0
                    # Queue full state broadcast
                    if update_queue:
                        update_queue.put_nowait(list(ne_mem))
                    print(f"[WS] {client_addr}: Reset all NE bytes to 0")
            except Exception as e:
                print(f"[WS] Error from {client_addr}: {e}")
    except Exception as e:
        if "keepalive ping timeout" not in str(e).lower():
            print(f"[WS] Connection error {client_addr}: {e}")
    finally:
        print(f"[WS] Client disconnected: {client_addr}")
        with ws_lock:
            ws_clients.discard(websocket)


def start_websocket_server(ne_mem: List[int], port: int = 8001):
    """Start WebSocket server in a separate thread."""
    global ws_loop
    loop = asyncio.new_event_loop()
    ws_loop = loop
    
    async def serve():
        global update_queue
        update_queue = asyncio.Queue()
        
        # Start update broadcaster
        asyncio.create_task(update_broadcaster(ne_mem))
        
        async with websockets.serve(
            partial(websocket_handler, ne_mem=ne_mem),
            "0.0.0.0",  # Listen on all interfaces
            port
        ):
            await asyncio.Future()  # run forever
    
    def run_async():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(serve())
    
    thread = threading.Thread(target=run_async, daemon=True)
    thread.start()


async def update_broadcaster(ne_mem: List[int]):
    """Broadcast updates from the queue to all WebSocket clients."""
    while True:
        try:
            # Wait for updates with a timeout to periodically check clients
            data = await asyncio.wait_for(update_queue.get(), timeout=1.0)
            await broadcast_state(data)
        except asyncio.TimeoutError:
            # Send periodic heartbeat/state to keep connections alive
            with ne_lock:
                data = list(ne_mem)
            if ws_clients:
                await broadcast_state(data)


def generate_html() -> str:
    html_path = Path(__file__).with_name("index.html")
    try:
        return html_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "<html><body><h2>index.html not found</h2></body></html>"


def start_http_server(ne_mem: List[int], port: int = 8000):
    print(f"[DEBUG] start_http_server: ne_mem id = {id(ne_mem)}, len = {len(ne_mem)}")
    
    class Handler(BaseHTTPRequestHandler):
        def _set_headers(self, code=200, content_type="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._set_headers(200, "text/html; charset=utf-8")
                self.wfile.write(generate_html().encode())
            elif self.path == "/state":
                with ne_lock:
                    data = list(ne_mem)
                self._set_headers()
                self.wfile.write(json.dumps(data).encode())
            else:
                self._set_headers(404)

        def do_POST(self):
            if self.path == "/write":
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                try:
                    payload = json.loads(body or b"{}")
                    idx = int(payload.get("index"))
                    val = int(payload.get("value")) & 0xFF
                except Exception:
                    self._set_headers(400)
                    return
                if 0 <= idx < NE_SIZE:
                    with ne_lock:
                        ne_mem[idx] = val
                self._set_headers(200)
                self.wfile.write(b"OK")
            else:
                self._set_headers(404)

    server = HTTPServer(("", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Automatically select port based on privileges
    if len(sys.argv) > 1:
        port = int(sys.argv[1])
    else:
        # Check if running with elevated privileges
        try:
            # Try to bind to port 502 to test privileges
            import socket
            test_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            test_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            test_socket.bind(('', 502))
            test_socket.close()
            port = 502  # We have privileges, use standard Modbus port
        except (OSError, PermissionError):
            port = 1502  # No privileges, use high port
    
    ne_memory: List[int] = [0] * NE_SIZE
    print(f"[DEBUG] main: ne_memory id = {id(ne_memory)}, len = {len(ne_memory)}")
    
    # Print banner
    print("=" * 60)
    print("ISY-GLT Merker Monitor - Modbus/TCP Server")
    print("=" * 60)

    store = ModbusSlaveContext(
        di=ModbusSequentialDataBlock(0, [0] * COIL_COUNT),  # unused
        co=NECoilBlock(ne_memory),
        hr=NERegisterBlock(ne_memory),
        ir=ModbusSequentialDataBlock(0, [0] * REG_COUNT),  # unused
    )
    context = ModbusServerContext(slaves=store, single=True)

    identity = ModbusDeviceIdentification()
    identity.VendorName = "ISYGLT"
    identity.ProductCode = "IPMS"
    identity.VendorUrl = "https://www.isyglt.com"
    identity.ProductName = "Dummy ISY-GLT IP-Master"
    identity.ModelName = "Python Modbus Emulator"
    identity.MajorMinorRevision = "1.0"

    load_labels() # Load labels on startup
    load_state(ne_memory) # Load state on startup
    load_bit_labels()

    start_monitor(ne_memory)
    
    # launch Modbus server in a daemon thread
    server_thread = threading.Thread(
        target=StartTcpServer,
        kwargs={"context": context, "identity": identity, "address": ("", port)},
        daemon=True,
    )
    server_thread.start()

    print(f"\n📡 Modbus/TCP server: port {port}")
    if port == 502:
        print(f"   ✓ Using standard Modbus port (running with privileges)")
    elif port == 1502:
        print(f"   → Using high port (run with sudo/admin for port 502)")
    print(f"   - Coils: 0-2047 → NE0.1 to NE255.8")
    print(f"   - Holding Registers: 0-127 → NE0+NE1 to NE254+NE255")
    
    start_http_server(ne_memory, 8000)
    
    # Small delay to ensure everything is initialized
    time.sleep(0.5)
    start_websocket_server(ne_memory, 8001)
    
    print(f"\n🌐 Web interface: http://localhost:8000/")
    print(f"   - Real-time updates via WebSocket")
    print(f"   - Left-click bits for momentary, right-click to toggle")
    print(f"   - Labels saved to: {LABELS_FILE}")
    print(f"   - State saved to: {STATE_FILE} (auto-save every 30s)")
    print(f"\n✅ All services running. Press Ctrl+C to exit.\n")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n\n🛑 Shutting down...")
        save_labels() # Save labels on exit
        save_state(ne_memory) # Save state on exit
        print("✅ Labels and state saved. Goodbye!")


if __name__ == "__main__":
    main() 
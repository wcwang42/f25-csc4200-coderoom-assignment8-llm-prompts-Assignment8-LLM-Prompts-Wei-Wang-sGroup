#!/usr/bin/env python3
"""
UDP Overlay Networking — Reference Solution (Instructor)
-------------------------------------------------------
- Peer discovery via UDP broadcast [PEER_SYNC]
- Liveness via [PING]/[PONG]
- Periodic pruning of inactive peers
- Summary printer for debugging
- Lightweight model update message flow ([MODEL_META] + [MODEL_CHUNK])

Packet format (ASCII, one line per datagram):
V=1|SRC=<node_id>|SEQ=<int>|TYPE=<msgtype>|TS=<epoch_ms>|BODY=<opaque>

Msg types and bodies:
- PEER_SYNC   BODY: ip=<a.b.c.d>;port=<int>
- PING        BODY: t0=<epoch_ms>
- PONG        BODY: t0=<epoch_ms>
- MODEL_META  BODY: ver=<str>;size=<int>;chunks=<int>;sha256=<hex>
- MODEL_CHUNK BODY: ver=<str>;idx=<int>;total=<int>;b64=<...>

Notes for Raspberry Pi:
- Uses only Python stdlib; no heavy dependencies
- Socket configured with SO_BROADCAST and SO_REUSEADDR
- Local IP detection uses UDP connect() trick (no packets sent)

This file implements the full solution requested by the instructor.
Keep function signatures intact so student skeletons remain compatible.
"""

import base64
import socket
import threading
import time
from typing import Tuple

# ---------- Global Configuration ----------
PORT = 5000
BROADCAST_IP = "255.255.255.255"
SYNC_INTERVAL = 5
PING_INTERVAL = 15
PING_TIMEOUT = 3
REMOVE_TIMEOUT = 30


class PeerNode:
    """
    Represents a single node in the decentralized chatbot overlay.
    Implements UDP broadcast discovery, heartbeat, and model-update messages.
    """

    def __init__(self, node_id: str):
        """Initialize node state, socket, and peer table."""
        self.id = node_id
        self.ip = None
        self.port = PORT
        self.seq = 0
        self.sock = None
        # peers: {peer_id: {"ip": str, "port": int, "last_seen": float, "rtt": float|None,
        #                   "model_ver": str|None, "chunks_recv": int|None, "chunks_total": int|None}}
        self.peers = {}
        self.running = False
        self.lock = threading.Lock()
        # rudimentary in-memory model update buffer
        self._model_buffers = {}  # {ver: {"total": int, "parts": dict[idx->bytes], "sha256": str}}

        self._setup_socket()
        self.ip = self._get_local_ip()

    # ---------- Setup ----------
    def _setup_socket(self):
        """Create and configure UDP socket for broadcast and unicast."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except Exception:
            pass  # not available on all platforms
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind(("", PORT))  # listen on all interfaces
        self.sock = s

    def _get_local_ip(self):
        """Return the local IP address of this host."""
        ip = "127.0.0.1"
        try:
            tmp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                tmp.connect(("8.8.8.8", 80))  # no packets actually sent
                ip = tmp.getsockname()[0]
            finally:
                tmp.close()
        except Exception:
            pass
        return ip

    # ---------- Message Handling ----------
    def _next_seq(self):
        """Increment and return the next sequence number."""
        with self.lock:
            self.seq += 1
            return self.seq

    def _make_packet(self, msgtype: str, body: str) -> str:
        """Build packet string according to format spec."""
        ts_ms = int(time.time() * 1000)
        seq = self._next_seq()
        fields = [
            f"V=1",
            f"SRC={self.id}",
            f"SEQ={seq}",
            f"TYPE={msgtype}",
            f"TS={ts_ms}",
            f"BODY={body}",
        ]
        return "|".join(fields)

    def _send(self, data: str, addr: Tuple[str, int]):
        """Send encoded UDP packet to destination."""
        try:
            self.sock.sendto(data.encode("utf-8"), addr)
        except Exception as e:
            print(f"[ERROR] send to {addr} failed: {e}")

    # ---------- Overlay Operations ----------
    def broadcast_sync(self):
        """Broadcast [PEER_SYNC] message to announce presence."""
        body = f"ip={self.ip};port={self.port}"
        pkt = self._make_packet("PEER_SYNC", body)
        self._send(pkt, (BROADCAST_IP, PORT))

    def send_ping(self, peer_id: str, peer_info: dict):
        """Send [PING] message to a specific peer."""
        t0 = int(time.time() * 1000)
        body = f"t0={t0}"
        pkt = self._make_packet("PING", body)
        self._send(pkt, (peer_info["ip"], peer_info["port"]))

    def send_pong(self, addr: Tuple[str, int]):
        """Send [PONG] message back to sender."""
        # echo original timestamp if present
        body = f"t0={int(time.time() * 1000)}"
        pkt = self._make_packet("PONG", body)
        self._send(pkt, addr)

    # --- Model update helpers (optional hooks for TinyLlama weights over LAN) ---
    def _announce_model_meta(self, ver: str, size: int, chunks: int, sha256_hex: str):
        body = f"ver={ver};size={size};chunks={chunks};sha256={sha256_hex}"
        pkt = self._make_packet("MODEL_META", body)
        self._send(pkt, (BROADCAST_IP, PORT))

    def _send_model_chunk(self, ver: str, idx: int, total: int, raw_bytes: bytes, dst: Tuple[str, int]):
        b64 = base64.b64encode(raw_bytes).decode("ascii")
        body = f"ver={ver};idx={idx};total={total};b64={b64}"
        pkt = self._make_packet("MODEL_CHUNK", body)
        self._send(pkt, dst)

    # -------------------------------------------------------------------------
    def handle_message(self, msg: str, addr: Tuple[str, int]):
        """Parse and process incoming UDP packet."""
        try:
            parts = msg.strip().split("|")
            kv = {}
            for p in parts:
                if "=" in p:
                    k, v = p.split("=", 1)
                    kv[k] = v
            if kv.get("V") != "1":
                return
            src = kv.get("SRC")
            if not src or src == self.id:
                return  # ignore our own packets or malformed
            mtype = kv.get("TYPE", "")
            body = kv.get("BODY", "")

            # update peer last_seen
            now = time.time()
            with self.lock:
                peer = self.peers.get(src)
                if not peer:
                    self.peers[src] = {
                        "ip": addr[0],
                        "port": addr[1],
                        "last_seen": now,
                        "rtt": None,
                        "model_ver": None,
                        "chunks_recv": None,
                        "chunks_total": None,
                    }
                else:
                    peer["last_seen"] = now
                    peer["ip"], peer["port"] = addr[0], addr[1]

            # handle types
            if mtype == "PEER_SYNC":
                # If we newly heard about this peer, we could respond with a ping to accelerate RTT learning
                pass

            elif mtype == "PING":
                self.send_pong(addr)

            elif mtype == "PONG":
                # compute RTT if they echoed t0
                t0 = None
                for token in body.split(";"):
                    if token.startswith("t0="):
                        try:
                            t0 = int(token.split("=", 1)[1])
                        except Exception:
                            t0 = None
                if t0 is not None:
                    rtt_ms = int(time.time() * 1000) - t0
                    with self.lock:
                        if src in self.peers:
                            self.peers[src]["rtt"] = rtt_ms

            elif mtype == "MODEL_META":
                # track intent to receive model version
                meta = {}
                for token in body.split(";"):
                    if "=" in token:
                        k, v = token.split("=", 1)
                        meta[k] = v
                ver = meta.get("ver")
                total = int(meta.get("chunks", "0") or 0)
                sha = meta.get("sha256", "")
                if ver and total > 0:
                    self._model_buffers.setdefault(ver, {"total": total, "parts": {}, "sha256": sha})
                    with self.lock:
                        self.peers[src]["model_ver"] = ver
                        self.peers[src]["chunks_recv"] = 0
                        self.peers[src]["chunks_total"] = total
                print(f"[MODEL] announced ver={ver} total={total} from {src}")

            elif mtype == "MODEL_CHUNK":
                fields = {}
                for token in body.split(";"):
                    if "=" in token:
                        k, v = token.split("=", 1)
                        fields[k] = v
                ver = fields.get("ver")
                idx = int(fields.get("idx", "-1"))
                total = int(fields.get("total", "0"))
                b64 = fields.get("b64", "")
                if ver is None or idx < 0:
                    return
                buf = self._model_buffers.setdefault(ver, {"total": total, "parts": {}, "sha256": ""})
                try:
                    buf["parts"][idx] = base64.b64decode(b64.encode("ascii"))
                    with self.lock:
                        if src in self.peers:
                            self.peers[src]["chunks_recv"] = len(buf["parts"])
                            self.peers[src]["chunks_total"] = total
                except Exception as e:
                    print(f"[MODEL] chunk decode error: {e}")
                # (Instructor note) Real implementation would verify SHA256 after reassembly and persist to disk.

            # else: ignore unknown types to preserve extensibility
        except Exception as e:
            print(f"[ERROR] handle_message failed from {addr}: {e}")

    # ---------- Thread Tasks ----------
    def listener(self):
        """Continuously listen for incoming packets."""
        while self.running:
            try:
                data, addr = self.sock.recvfrom(65535)
                msg = data.decode("utf-8", errors="ignore")
                self.handle_message(msg, addr)
            except OSError:
                break
            except Exception as e:
                print(f"[ERROR] listener: {e}")

    def broadcaster(self):
        """Periodically broadcast PEER_SYNC messages."""
        while self.running:
            self.broadcast_sync()
            time.sleep(SYNC_INTERVAL)

    def heartbeat(self):
        """Send pings and remove inactive peers."""
        while self.running:
            now = time.time()
            to_remove = []
            with self.lock:
                items = list(self.peers.items())
            for pid, info in items:
                # ping if due
                if (now - info["last_seen"]) >= PING_INTERVAL:
                    self.send_ping(pid, info)
                # prune if stale
                if (now - info["last_seen"]) >= REMOVE_TIMEOUT:
                    to_remove.append(pid)
            if to_remove:
                with self.lock:
                    for pid in to_remove:
                        self.peers.pop(pid, None)
                        print(f"[PRUNE] removed {pid}")
            time.sleep(1)

    def summary(self):
        """Print peer-table summary periodically."""
        while self.running:
            with self.lock:
                peers_copy = dict(self.peers)
            if peers_copy:
                print("\n[SUMMARY] Peers:")
                for pid, p in peers_copy.items():
                    age = int(time.time() - p["last_seen"]) if p.get("last_seen") else -1
                    rtt = p.get("rtt")
                    mv = p.get("model_ver")
                    cr = p.get("chunks_recv")
                    ct = p.get("chunks_total")
                    extra = f" rtt={rtt}ms" if rtt is not None else ""
                    if mv:
                        extra += f" | model={mv} {cr}/{ct}"
                    print(f" - {pid}@{p['ip']}:{p['port']} seen={age}s ago{extra}")
            else:
                print("\n[SUMMARY] No peers known yet.")
            time.sleep(10)

    # ---------- Control ----------
    def start(self):
        """Start listener, broadcaster, heartbeat, and summary threads."""
        if self.running:
            return
        self.running = True
        self._threads = [
            threading.Thread(target=self.listener, daemon=True),
            threading.Thread(target=self.broadcaster, daemon=True),
            threading.Thread(target=self.heartbeat, daemon=True),
            threading.Thread(target=self.summary, daemon=True),
        ]
        for t in self._threads:
            t.start()
        print(f"[START] {self.id} on {self.ip}:{self.port}")

    def stop(self):
        """Stop all threads and close socket."""
        if not self.running:
            return
        self.running = False
        try:
            # poke the socket to unblock recvfrom if needed
            self.sock.settimeout(0.1)
        except Exception:
            pass
        for t in getattr(self, "_threads", []):
            t.join(timeout=1)
        try:
            self.sock.close()
        except Exception:
            pass
        print("[STOP] node stopped")


# ---------- Entry Point ----------
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Usage: python node_interface.py <node_id>")
        exit(1)

    node_id = sys.argv[1]
    node = PeerNode(node_id)
    node.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        node.stop()
        print("\n[EXIT] Node stopped.")


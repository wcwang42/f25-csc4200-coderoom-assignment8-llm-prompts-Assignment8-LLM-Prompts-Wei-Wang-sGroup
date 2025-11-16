#!/usr/bin/env python3
"""
udp_overlay.py — Decentralized UDP overlay with chunked model-delta exchange
---------------------------------------------------------------------------
• Peer discovery via broadcast
• PING/PONG heartbeat
• Multi-chunk MODEL_META / MODEL_CHUNK transfer (safe for >64 KB deltas)
• Knowledge queries removed (model now handles answers)
"""

import base64, socket, threading, time
from typing import Tuple

PORT = 5000
BROADCAST_IP = "255.255.255.255"
SYNC_INTERVAL = 5
PING_INTERVAL = 15
REMOVE_TIMEOUT = 30
MAX_UDP = 60000  # ~60 KB safe payload


class PeerNode:
    def __init__(self, node_id: str, debug: bool = False):
        self.id = node_id
        self.ip = self._get_local_ip()
        self.port = PORT
        self.seq = 0
        self.sock = None
        self.peers = {}
        self._model_buffers = {}  # {ver:{total:int,parts:dict,sha256:str}}
        self.lock = threading.Lock()
        self.running = False
        self.debug = debug
        self._setup_socket()

    # ---------- setup ----------
    def _setup_socket(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind(("", PORT))
        self.sock = s

    def _get_local_ip(self):
        ip = "127.0.0.1"
        try:
            tmp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            tmp.connect(("8.8.8.8", 80))
            ip = tmp.getsockname()[0]
            tmp.close()
        except Exception:
            pass
        return ip

    # ---------- packet helpers ----------
    def _next_seq(self):
        self.seq += 1
        return self.seq

    def _make_packet(self, mtype, body):
        ts = int(time.time() * 1000)
        return f"V=1|SRC={self.id}|SEQ={self._next_seq()}|TYPE={mtype}|TS={ts}|BODY={body}"

    def _send(self, data, addr):
        try:
            self.sock.sendto(data.encode(), addr)
            if self.debug:
                mtype = data.split("|TYPE=")[1].split("|", 1)[0]
                print(f"[SEND] {mtype} → {addr[0]}")
        except Exception as e:
            print(f"[ERROR] send: {e}")

    # ---------- overlay ----------
    def broadcast_sync(self):
        body = f"ip={self.ip};port={self.port}"
        pkt = self._make_packet("PEER_SYNC", body)
        self._send(pkt, (BROADCAST_IP, PORT))

    def _announce_model_meta(self, ver, size, chunks, sha):
        body = f"ver={ver};size={size};chunks={chunks};sha256={sha}"
        pkt = self._make_packet("MODEL_META", body)
        self._send(pkt, (BROADCAST_IP, PORT))

    def _send_model_chunk(self, ver, idx, total, raw_bytes, dst):
        # Encode safely in base64 and split automatically
        b64 = base64.b64encode(raw_bytes).decode()
        body = f"ver={ver};idx={idx};total={total};b64={b64}"
        if len(body) > MAX_UDP:
            # further fragment to avoid 90 error
            frag_total = (len(b64) + MAX_UDP - 1) // MAX_UDP
            for j in range(frag_total):
                frag = b64[j * MAX_UDP : (j + 1) * MAX_UDP]
                sub_body = f"ver={ver};idx={idx}.{j};total={total}.{frag_total};b64={frag}"
                pkt = self._make_packet("MODEL_FRAG", sub_body)
                self._send(pkt, dst)
                time.sleep(0.005)
        else:
            pkt = self._make_packet("MODEL_CHUNK", body)
            self._send(pkt, dst)

    # ---------- receive ----------
    def handle_message(self, msg, addr):
        try:
            parts = msg.split("|")
            kv = {k: v for k, v in (p.split("=", 1) for p in parts if "=" in p)}
            src, mtype, body = kv.get("SRC", "?"), kv.get("TYPE", "?"), kv.get("BODY", "")
            if src == self.id:
                return

            now = time.time()
            new_peer = src not in self.peers
            self.peers[src] = {"ip": addr[0], "port": addr[1], "last_seen": now}
            if new_peer:
                print(f"[PEER] discovered {src} @ {addr[0]}")

            # ----- model update handling -----
            if mtype == "MODEL_META":
                meta = dict(x.split("=", 1) for x in body.split(";") if "=" in x)
                ver, total, sha = meta["ver"], int(meta["chunks"]), meta["sha256"]
                self._model_buffers.setdefault(ver, {"total": total, "parts": {}, "sha256": sha})
                print(f"[DELTA-META] from {src}: ver={ver} chunks={total}")

            elif mtype in ("MODEL_CHUNK", "MODEL_FRAG"):
                fields = dict(x.split("=", 1) for x in body.split(";") if "=" in x)
                ver = fields.get("ver")
                idx_field = fields.get("idx", "0")
                total_field = fields.get("total", "1")
                b64 = fields.get("b64", "")
                if "." in idx_field:
                    # fragment: idx=chunk.frag
                    chunk_idx, frag_idx = map(int, idx_field.split("."))
                    chunk_total, frag_total = map(int, total_field.split("."))
                    buf = self._model_buffers.setdefault(ver, {"total": chunk_total, "parts": {}, "sha256": ""})
                    frag_key = f"{chunk_idx}.{frag_idx}"
                    buf["parts"][frag_key] = b64
                    # check if all fragments of this chunk arrived
                    frag_prefix = f"{chunk_idx}."
                    frags = [v for k, v in buf["parts"].items() if k.startswith(frag_prefix)]
                    if len(frags) == frag_total:
                        data = base64.b64decode("".join(frags))
                        buf["parts"][chunk_idx] = data
                        # clean up fragments
                        for k in list(buf["parts"].keys()):
                            if isinstance(k, str) and k.startswith(frag_prefix):
                                del buf["parts"][k]
                        print(f"[DELTA-RECV] {ver} chunk {chunk_idx+1}/{chunk_total} reassembled from {src}")
                else:
                    # normal chunk
                    idx = int(idx_field)
                    total = int(total_field)
                    buf = self._model_buffers.setdefault(ver, {"total": total, "parts": {}, "sha256": ""})
                    buf["parts"][idx] = base64.b64decode(b64.encode())
                    if len(buf["parts"]) == total:
                        print(f"[DELTA-RECV] full model {ver} ({total} chunks) received from {src}")

        except Exception as e:
            if self.debug:
                print(f"[ERROR] handle: {e}")

    # ---------- threads ----------
    def listener(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(65535)
                self.handle_message(data.decode(errors="ignore"), addr)
            except Exception:
                continue

    def broadcaster(self):
        while self.running:
            self.broadcast_sync()
            time.sleep(SYNC_INTERVAL)

    # ---------- control ----------
    def start(self):
        self.running = True
        threading.Thread(target=self.listener, daemon=True).start()
        threading.Thread(target=self.broadcaster, daemon=True).start()
        print(f"[START] {self.id} on {self.ip}:{self.port}")

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except Exception:
            pass
        print("[STOP] node stopped")


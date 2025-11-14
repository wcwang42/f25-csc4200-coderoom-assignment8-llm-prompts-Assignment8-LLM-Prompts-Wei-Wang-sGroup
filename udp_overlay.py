#!/usr/bin/env python3
"""
Assignment 8 — UDP Overlay for Distributed Training
---------------------------------------------------
In this assignment you will implement the *networking* portion of the
distributed model-update system.

You will:
  • Send MODEL_META announcements
  • Fragment large MODEL_CHUNK messages
  • Reassemble received fragments/chunks
  • Integrate your code with the peer overlay

The model code (TinyGPT) is provided separately.
DO NOT modify any ML-related components — focus ONLY on networking.

All TODO blocks must be completed.
"""

import base64, socket, threading, time
from typing import Tuple

PORT = 5000
BROADCAST_IP = "255.255.255.255"
SYNC_INTERVAL = 5
PING_INTERVAL = 15
REMOVE_TIMEOUT = 30

# You MUST enforce this limit to prevent "[Errno 90] Message too long"
MAX_UDP = 60000


class PeerNode:
    def __init__(self, node_id: str, debug: bool = False):
        self.id = node_id
        self.ip = self._get_local_ip()
        self.port = PORT
        self.seq = 0
        self.sock = None

        # discovered peers
        self.peers = {}

        # delta buffers: {ver: {"total": N, "parts": {i:bytes}, "sha256": str}}
        self._model_buffers = {}

        self.lock = threading.Lock()
        self.running = False
        self.debug = debug

        self._setup_socket()

    # -----------------------------------------------------------
    # Setup
    # -----------------------------------------------------------
    def _setup_socket(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.bind(("", PORT))
        self.sock = s

    def _get_local_ip(self):
        # No need to change
        ip = "127.0.0.1"
        try:
            tmp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            tmp.connect(("8.8.8.8", 80))
            ip = tmp.getsockname()[0]
            tmp.close()
        except:
            pass
        return ip

    # -----------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------
    def _next_seq(self):
        self.seq += 1
        return self.seq

    def _make_packet(self, mtype, body):
        ts = int(time.time() * 1000)
        return f"V=1|SRC={self.id}|SEQ={self._next_seq()}|TYPE={mtype}|TS={ts}|BODY={body}"

    def _send(self, data, addr):
        """
        Provided to you — DO NOT edit.
        Students must call this inside their implementations.
        """
        try:
            self.sock.sendto(data.encode(), addr)
            if self.debug:
                header = data.split("|TYPE=")[1].split("|")[0]
                print(f"[SEND] {header} → {addr[0]}")
        except Exception as e:
            print(f"[ERROR] send: {e}")

    # -----------------------------------------------------------
    # Part 1 — Announce model metadata (Provided)
    # -----------------------------------------------------------
    def announce_model_meta(self, ver, size, chunks, sha):
        """
        You DO NOT implement this. Provided for use.
        """
        body = f"ver={ver};size={size};chunks={chunks};sha256={sha}"
        pkt = self._make_packet("MODEL_META", body)
        self._send(pkt, (BROADCAST_IP, PORT))

    # -----------------------------------------------------------
    # Part 2 — Fragmentation (STUDENT IMPLEMENTS)
    # -----------------------------------------------------------
    def send_model_file(self, ver, filepath, addr):
        """
        TODO — IMPLEMENT THIS (5 points)
        --------------------------------
        You must:
          1. Read the file in binary.
          2. Split into <= MAX_UDP chunks.
          3. Base64 encode each chunk.
          4. Build:
                TYPE=MODEL_CHUNK
                BODY="ver=<ver>;idx=<i>;total=<n>;b64=<payload>"
          5. Send with self._send()

        Requirements:
          - Print a header for every fragment:
                [FRAG] ver=<ver> idx=<i>/<n> size=<bytes>
        """
        raise NotImplementedError("Students: implement file fragmentation + sending")

    # -----------------------------------------------------------
    # Part 3 — Reassembly (STUDENT IMPLEMENTS)
    # -----------------------------------------------------------
    def handle_model_chunk(self, body, src):
        """
        TODO — IMPLEMENT THIS
        --------------------------------
        body example: "ver=v123;idx=0;total=5;b64=AAABBBCCC..."
        
        You must:
          1. Parse ver, idx, total, b64.
          2. Decode base64.
          3. Store chunk in self._model_buffers.
          4. Print header:
                [RECV] ver=<ver> idx=<i>/<n> from=<src>
          5. If all chunks received:
                a. Reassemble bytes in order.
                b. Compute SHA256.
                c. Compare to stored sha256.
                d. Print either:
                       [OK] SHA verified for <ver>
                       [WARN] SHA mismatch for <ver>
        """
        raise NotImplementedError("Students: implement chunk parse + reassembly")

    # -----------------------------------------------------------
    # Part 4 — Message Dispatcher (Provided)
    # -----------------------------------------------------------
    def handle_message(self, msg, addr):
        """
        Provided skeleton. Students DO NOT modify packet routing logic.
        They ONLY implement handle_model_chunk().
        """
        try:
            parts = msg.split("|")
            kv = {k: v for k, v in (p.split("=", 1)
                                    for p in parts if "=" in p)}
            src = kv.get("SRC")
            mtype = kv.get("TYPE")
            body = kv.get("BODY", "")

            if src == self.id:
                return

            # update peer table
            self.peers[src] = {"ip": addr[0], "last_seen": time.time()}

            if mtype == "MODEL_META":
                meta = dict(x.split("=", 1)
                            for x in body.split(";") if "=" in x)
                ver = meta["ver"]
                chunks = int(meta["chunks"])
                sha = meta["sha256"]
                self._model_buffers[ver] = {"total": chunks,
                                            "parts": {},
                                            "sha256": sha}
                print(f"[DELTA-META] from={src} ver={ver} chunks={chunks}")

            elif mtype == "MODEL_CHUNK":
                self.handle_model_chunk(body, src)

        except Exception as e:
            print(f"[ERROR] handle_message: {e}")

    # -----------------------------------------------------------
    # Threads
    # -----------------------------------------------------------
    def listener(self):
        while self.running:
            try:
                data, addr = self.sock.recvfrom(65535)
                self.handle_message(data.decode(), addr)
            except:
                continue

    def broadcaster(self):
        while self.running:
            pkt = self._make_packet("PEER_SYNC",
                                    f"ip={self.ip};port={self.port}")
            self._send(pkt, (BROADCAST_IP, PORT))
            time.sleep(SYNC_INTERVAL)

    # -----------------------------------------------------------
    # Start & Stop
    # -----------------------------------------------------------
    def start(self):
        self.running = True
        threading.Thread(target=self.listener,
                         daemon=True).start()
        threading.Thread(target=self.broadcaster,
                         daemon=True).start()
        print(f"[START] {self.id} on {self.ip}:{self.port}")

    def stop(self):
        self.running = False
        try:
            self.sock.close()
        except:
            pass
        print("[STOP] node stopped")


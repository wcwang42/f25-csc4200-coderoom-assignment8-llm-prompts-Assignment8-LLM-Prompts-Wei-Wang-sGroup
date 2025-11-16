#!/usr/bin/env python3
"""
Assignment 7 — UDP Fragmentation & Reassembly over Overlay
-----------------------------------------------------------
Goal:
    Extend your PeerNode overlay to handle large binary transfers
    (e.g., model deltas).
"""

import os
import time
import base64
import hashlib
import tempfile
from typing import Tuple
from udp_overlay import PeerNode, BROADCAST_IP, PORT

MAX_UDP = 1480


def create_dummy_delta(size_bytes: int = 300000) -> Tuple[str, str, int]:
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
    data = os.urandom(size_bytes)
    tmp.write(data)
    tmp.close()
    sha = hashlib.sha256(data).hexdigest()
    return tmp.name, sha, size_bytes


def announce_model_meta(node: PeerNode, ver: str, size: int, chunks: int, sha: str):
    # write META body to a temp file so we can reuse fragment_and_send()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".meta")
    tmp.write(f"ver={ver};size={size};chunks={chunks};sha256={sha}".encode())
    tmp.close()
    fragment_and_send(node, ver, tmp.name, (BROADCAST_IP, PORT))
    os.remove(tmp.name)



def fragment_and_send(node: PeerNode, ver: str, filepath: str, addr: Tuple[str, int]):
    with open(filepath, "rb") as f:
        data = f.read()

    total = (len(data) + MAX_UDP - 1) // MAX_UDP

    for i in range(total):
        part = data[i * MAX_UDP : (i + 1) * MAX_UDP]
        b64 = base64.b64encode(part).decode()

        body = f"ver={ver};idx={i};total={total};b64={b64}"
        pkt = node._make_packet("MODEL_CHUNK", body)

        size_bytes = len(part)
        print(f"[SEND] TYPE=MODEL_CHUNK SRC={node.id} IDX={i+1}/{total} SIZE={size_bytes}")
        node._send(pkt, addr)

def receive_and_reassemble(node: PeerNode, ver: str, msg: str):
    # parse message
    parts = msg.split("|")
    kv = {k: v for k, v in (p.split("=", 1) for p in parts if "=" in p)}
    body = kv["BODY"]

    f = dict(x.split("=", 1) for x in body.split(";") if "=" in x)

    idx   = int(f["idx"])
    total = int(f["total"])
    b64   = f["b64"]

    chunk = base64.b64decode(b64)

    # create buffer for this version if needed
    if ver not in node._model_buffers:
        node._model_buffers[ver] = {
            "total": total,
            "parts": {}
        }

    buf = node._model_buffers[ver]
    buf["parts"][idx] = chunk

    print(f"[RECV] TYPE=MODEL_CHUNK VER={ver} IDX={idx+1}/{total}")

    # not all parts yet
    if len(buf["parts"]) < total:
        return None

    # all chunks present → reassemble
    ordered = [buf["parts"][i] for i in range(total)]
    full = b"".join(ordered)

    return full




def handle_incoming_chunk_reassemble(node: PeerNode, msg: str, addr: Tuple[str, int]):
    parts = msg.split("|")
    kv = {k: v for k, v in (p.split("=", 1) for p in parts if "=" in p)}
    body = kv["BODY"]

    f = dict(x.split("=", 1) for x in body.split(";") if "=" in x)

    ver   = f["ver"]
    idx   = int(f["idx"])
    total = int(f["total"])
    b64   = f["b64"]

    chunk = base64.b64decode(b64)

    if ver not in node._model_buffers:
        node._model_buffers[ver] = {
            "total": total,
            "parts": {},
            "_last_idx": 0
        }

    buf = node._model_buffers[ver]
    buf["parts"][idx] = chunk
    buf["_last_idx"]  = idx

    print(f"[RECV] TYPE=MODEL_CHUNK VER={ver} IDX={idx+1}/{total}")

    if len(buf["parts"]) < total:
        return None, None

    data = b"".join(buf["parts"][i] for i in range(total))
    return data, buf.get("sha256")




def handle_incoming_chunk(node: PeerNode, msg: str, addr: Tuple[str, int]):
    parts = msg.split("|")
    kv = {k: v for k, v in (p.split("=", 1) for p in parts if "=" in p)}
    body = kv["BODY"]

    f = dict(x.split("=", 1) for x in body.split(";") if "=" in x)

    ver = f["ver"]
    idx = int(f["idx"])
    total = int(f["total"])
    b64 = f["b64"]

    chunk = base64.b64decode(b64)

    if ver not in node._model_buffers:
        return

    buf = node._model_buffers[ver]
    buf["parts"][idx] = chunk

    print(f"[RECV] TYPE=MODEL_CHUNK VER={ver} IDX={idx+1}/{total}")

    if len(buf["parts"]) != total:
        return

    ordered = [buf["parts"][i] for i in range(total)]
    full = b"".join(ordered)

    sha_local = hashlib.sha256(full).hexdigest()

    if sha_local == buf["sha256"]:
        print(f"[OK] SHA verified for {ver}")
    else:
        print(f"[WARN] SHA mismatch for {ver}")



if __name__ == "__main__":
    node = PeerNode("Pi-1")
    node._model_buffers = {}

    path, sha, size = create_dummy_delta(300000)
    version = f"v{int(time.time())}"
    total_chunks = (size + MAX_UDP - 1) // MAX_UDP
    print(f"[INIT] Created delta {version} ({size} bytes, {total_chunks} chunks)")

    announce_model_meta(node, version, size, total_chunks, sha)
    node._model_buffers[version] = {"total": total_chunks, "parts": {}, "sha256": sha}

    print("\n[STEP 3] Sending delta in fragments …")
    fragment_and_send(node, version, path, (BROADCAST_IP, PORT))

    print("\n[STEP 4] Simulating receive and reassembly …")
    with open(path, "rb") as f:
        data = f.read()
    total = (len(data) + MAX_UDP - 1) // MAX_UDP
    for i in range(total):
        part = data[i * MAX_UDP : (i + 1) * MAX_UDP]
        b64 = base64.b64encode(part).decode()
        body = f"ver={version};idx={i};total={total};b64={b64}"
        msg = node._make_packet("MODEL_CHUNK", body)
        header = f"[RECV] TYPE=MODEL_CHUNK VER={version} IDX={i+1}/{total}"
        print(header)
        handle_incoming_chunk(node, msg, ("127.0.0.1", PORT))

    print("\n[TEST DONE] If implemented correctly, you should see:")
    print("  • '[SEND]' logs for each fragment")
    print("  • '[RECV]' header lines showing chunk numbers")
    print("  • '[OK] SHA verified' once all chunks received")



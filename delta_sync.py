#!/usr/bin/env python3
"""
delta_sync.py — model-delta creation, broadcast and merge helpers
Used by prompt_node.py with udp_overlay.py
"""

import os, io, torch, hashlib, tempfile, time
from udp_overlay import PeerNode, PORT
from update_exchanges import (
    announce_model_meta,
    fragment_and_send,
    handle_incoming_chunk,
    receive_and_reassemble,
)

# ---------- create and send deltas ----------
def export_delta(model, threshold=1e-6):
    """Compute sparse delta from current weights vs base.pt."""
    base = torch.load("base.pt", map_location="cpu")
    now = model.state_dict()
    delta = {}
    for k, v in now.items():
        diff = (v - base[k])
        if torch.norm(diff) > threshold:
            delta[k] = diff.half()

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pt")
    torch.save(delta, tmp.name)
    sha = hashlib.sha256(open(tmp.name, "rb").read()).hexdigest()
    size = os.path.getsize(tmp.name)
    return tmp.name, sha, size


# ------------------------------------------------------------
#  broadcast your delta update to all peers
# ------------------------------------------------------------
def broadcast_delta(node: PeerNode, path: str, sha: str, size: int):
    """
    Broadcast the delta file to all peers in UDP-safe chunks.
    Announce metadata, then call your overlay’s fragmentation API.
    """
    # 1. Construct version identifier based on timestamp
    version = f"delta_v{int(time.time())}"

    # 2. Compute total chunks
    total_chunks = (size + node.MAX_UDP - 1) // node.MAX_UDP if hasattr(node, "MAX_UDP") else (
        (size + 44000 - 1) // 44000
    )

    print(f"[INIT] Created delta {version} ({size} bytes, {total_chunks} chunks)")
    print(f"[META] Announcing delta {version} ...")

    # 3. Announce metadata to all peers
    announce_model_meta(node, version, size, total_chunks, sha)

    # 4. Delay briefly to give peers time to register the metadata
    time.sleep(1)

    # 5. Actually send the file to peers
    # Use helper fragment_and_send provided by update_exchanges.py
    count = fragment_and_send(node, version, path)

    print(f"[BROADCAST] Delta {version} sent to {count} peer(s)")
    return version


# ------------------------------------------------------------
#  Reassemble an incoming delta from its chunks
# ------------------------------------------------------------
def reassemble_delta(node: PeerNode, ver: str):
    """
    Reassemble all received chunks of a given version,
    verify its SHA256 hash, and return the binary data (bytes) if valid.
    """
    if ver not in node._model_buffers:
        print(f"[WARN] reassemble_delta: no buffer for {ver}")
        return None

    buf = node._model_buffers[ver]
    total = buf["total"]
    parts = buf["parts"]
    sha_expected = buf.get("sha256", None)

    # Verify all chunks are present
    if len(parts) < total:
        print(f"[REASSEMBLE] {ver}: incomplete ({len(parts)}/{total}) — waiting for more")
        return None

    print(f"[REASSEMBLE] Starting reassembly for {ver} ({total} chunks)")

    # Reassemble binary and verify SHA
    reassembled = b""
    for i in range(total):
        if i not in parts:
            print(f"[ERROR] Missing chunk {i} for {ver}")
            return None
        try:
            chunk_data = parts[i]
            chunk_bytes = base64.b64decode(chunk_data)
            reassembled += chunk_bytes
        except Exception as e:
            print(f"[ERROR] Failed to decode chunk {i}: {e}")
            return None

    sha_actual = hashlib.sha256(reassembled).hexdigest()
    if sha_expected:
        if sha_actual != sha_expected:
            print(f"[ERROR] SHA mismatch for {ver}\n  Expected: {sha_expected}\n  Actual:   {sha_actual}")
            return None
        else:
            print(f"[OK] SHA verified for {ver}")

    # (Optional) Save this delta to disk for inspection
    try:
        output_dir = os.path.join(os.getcwd(), "received_deltas")
        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, f"{ver}.pt")
        with open(path, "wb") as out:
            out.write(reassembled)
        print(f"[SAVE] Delta {ver} saved to {path}")
    except Exception as e:
        print(f"[WARN] Could not save delta {ver}: {e}")

    print(f"[TEST DONE] ✔ All fragments received and verified for {ver}")
    return reassembled


# ------------------------------------------------------------
#  Apply incoming deltas
# ------------------------------------------------------------
def apply_incoming_deltas(node: PeerNode, model, merge_weight=1.0):
    merged = 0

    for ver, buf in list(node._model_buffers.items()):
        # Skip if incomplete or already merged
        total = buf["total"]
        parts = buf["parts"]
        if len(parts) < total:
            continue

        data = reassemble_delta(node, ver)
        if data is None:
            continue

        # Write data into a temp PT file to load with torch
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pt")
        tmp.write(data)
        tmp.close()

        try:
            delta = torch.load(tmp.name, map_location="cpu", weights_only=False)
            sd = model.state_dict()
            applied = 0

            for k, v in delta.items():
                if k in sd and sd[k].shape == v.shape:
                    sd[k] = sd[k] + (merge_weight * v.to(sd[k].dtype))
                    applied += 1

            model.load_state_dict(sd)
            torch.save(model.state_dict(), "base.pt")
            print(f"[MERGE] model {ver} applied ✓ ({applied} layers)")
            merged += 1

        except Exception as e:
            print(f"[ERROR] failed to merge {ver}: {e}")
        finally:
            os.remove(tmp.name)
            node._model_buffers.pop(ver, None)

    return merged
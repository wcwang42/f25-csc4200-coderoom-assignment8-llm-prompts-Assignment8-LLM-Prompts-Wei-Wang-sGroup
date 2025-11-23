#!/usr/bin/env python3
"""
delta_sync.py — model-delta creation, broadcast and merge helpers
Used by prompt_node.py together with udp_overlay.py
"""

import os
import io
import base64
import torch
import hashlib
import tempfile
import time
from udp_overlay import PeerNode   # only this import is required


# ------------------------------------------------------------
#  export a model delta (already provided for you)
# ------------------------------------------------------------
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
#  broadcast a delta file using PeerNode’s built‑in overlay
# ------------------------------------------------------------
def broadcast_delta(node: PeerNode, path: str, sha: str, size: int):
    """
    Announce and broadcast a model delta file to all peers.
    Uses PeerNode’s internal announce_model_meta() and
    broadcast_file_to_all_peers() methods.
    """
    version = f"delta_v{int(time.time())}"
    total_chunks = (size + node.MAX_UDP - 1) // node.MAX_UDP

    print(f"[INIT] Created delta {version} ({size} bytes, {total_chunks} chunks)")
    print(f"[META] Announcing delta {version} ...")

    # 1. Announce metadata so peers prepare buffers
    node.announce_model_meta(version, size, total_chunks, sha)

    # 2. Short delay so listeners register META before chunks arrive
    time.sleep(1)

    # 3. Send the actual file in fragments
    count = node.broadcast_file_to_all_peers(version, path)
    print(f"[BROADCAST] Delta {version} sent to {count} peer(s)")
    return version


# ------------------------------------------------------------
#  Reassemble all received chunks for a delta version
# ------------------------------------------------------------
def reassemble_delta(node: PeerNode, ver: str):
    """
    Reassemble the binary data for a given version from node._model_buffers,
    verify its SHA256, optionally save to disk, and return the raw bytes.
    """
    if ver not in node._model_buffers:
        print(f"[WARN] reassemble_delta: no buffer for {ver}")
        return None

    buf = node._model_buffers[ver]
    total = buf["total"]
    parts = buf["parts"]
    sha_expected = buf.get("sha256")

    if len(parts) < total:
        print(f"[REASSEMBLE] {ver}: incomplete ({len(parts)}/{total}) — waiting")
        return None

    print(f"[REASSEMBLE] Starting reassembly for {ver} ({total} chunks)")
    reassembled = b''.join(base64.b64decode(parts[i]) for i in range(total) if i in parts)
    sha_actual = hashlib.sha256(reassembled).hexdigest()

    if sha_expected:
        if sha_actual != sha_expected:
            print(f"[ERROR] SHA mismatch for {ver}")
            print(f"  Expected: {sha_expected}")
            print(f"  Computed: {sha_actual}")
            return None
        else:
            print(f"[OK] SHA verified for {ver}")

    # Save the reassembled delta for record
    try:
        os.makedirs("received_deltas", exist_ok=True)
        path = os.path.join("received_deltas", f"{ver}.pt")
        with open(path, "wb") as f:
            f.write(reassembled)
        print(f"[SAVE] Delta {ver} saved to {path}")
    except Exception as e:
        print(f"[WARN] Could not save {ver}: {e}")

    print(f"[TEST DONE] ✔ All fragments received and verified for {ver}")
    return reassembled


# ------------------------------------------------------------
#  Merge any verified incoming deltas into the model
# ------------------------------------------------------------
def apply_incoming_deltas(node: PeerNode, model, merge_weight=1.0):
    """
    Inspect node._model_buffers for any fully received deltas,
    verify, merge them into the local model, and remove from buffer.
    """
    merged = 0

    for ver, buf in list(node._model_buffers.items()):
        if len(buf["parts"]) < buf["total"]:
            continue

        data = reassemble_delta(node, ver)
        if data is None:
            continue

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
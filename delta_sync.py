#!/usr/bin/env python3
"""
delta_sync.py — model-delta creation, broadcast and merge helpers
Used by prompt_node.py with udp_overlay.py
"""

import os, io, torch, hashlib, tempfile, time
from udp_overlay import BROADCAST_IP, PORT


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


def broadcast_delta(node, path, sha, size):
    """Announce and send a delta file over the overlay."""
    ver = f"v{int(time.time())}"
    chunks = 1
    node._announce_model_meta(ver, size, chunks, sha)
    data = open(path, "rb").read()
    node._send_model_chunk(ver, 0, chunks, data, (BROADCAST_IP, PORT))
    print(f"[SEND] delta {ver} ({size} B) broadcasted")
    os.remove(path)


# ---------- receive and apply deltas ----------
def apply_incoming_deltas(node, model, merge_weight=1.0):
    """Check node._model_buffers for complete deltas and merge them."""
    merged = 0
    for ver, buf in list(node._model_buffers.items()):
        total, parts, sha = buf["total"], buf["parts"], buf["sha256"]
        if len(parts) < total:
            continue  # still waiting

        # reassemble and verify SHA
        data = b"".join(parts[i] for i in sorted(parts))
        sha_local = hashlib.sha256(data).hexdigest()
        if sha_local != sha:
            print(f"[WARN] SHA mismatch for {ver}")
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
            torch.save(model.state_dict(), "base.pt")   # persist new base
            merged += 1
            print(f"[MERGE] model {ver} applied ✓ ({applied} layers)")
        except Exception as e:
            print(f"[ERROR] failed to merge {ver}: {e}")
        finally:
            os.remove(tmp.name)

        # clear buffer
        node._model_buffers.pop(ver, None)
    return merged


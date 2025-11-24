#!/usr/bin/env python3
"""
Unified UDP Overlay with File Transfer
---------------------------------------
Combined peer discovery, heartbeat protocol, and large file fragmentation/transfer
for decentralized LLM agent network.
"""
import socket
import threading
import time
import binascii
import sys
import os
import base64
import hashlib
import tempfile
from typing import Tuple, Optional

# ---------- Global Configuration ----------
PORT = 5000
SYNC_INTERVAL = 5
PING_INTERVAL = 15
PING_TIMEOUT = 3
REMOVE_TIMEOUT = 30
MAX_UDP = 4096  # Maximum raw binary bytes per chunk (before Base64 encoding)


class PeerNode:
    """
    Represents a single node in the decentralized chatbot overlay.
    Implements UDP broadcast discovery, heartbeat logic, and file transfer capabilities.
    """
    def __init__(self, node_id: str):
        """Initialize node state, socket, and peer table."""
        self.id = node_id
        self.ip = None
        self.broadcast_ip = None
        self.port = PORT
        self.seq = 0
        self.sock = None
        self.peers = {}          # {peer_id: {"ip":..., "port":..., "last_seen":...}}
        self.running = False
        self.lock = threading.Lock()
        self.ping_sent = {}      # Track ping timestamps for RTT calculation
        
        # File transfer buffers
        self._model_buffers = {}  # {version: {"total": N, "parts": {idx: b64_data}, "sha256": hash}}
        self._transfer_callbacks = {}  # {version: callback_function}
        
        # Metrics tracking
        self.metrics = {
            "pings_sent": 0,
            "pongs_received": 0,
            "rtt_samples": [],
            "syncs_sent": 0,
            "syncs_received": 0,
            "chunks_sent": 0,
            "chunks_received": 0,
            "transfers_received": 0
        }
        
        # Setup socket and get local IP
        self._setup_socket()
        self.ip = self._get_local_ip()
        self.broadcast_ip = self._get_broadcast_ip()
        print(f"[INIT] Node {self.id} ready at {self.ip}:{self.port}")
        print(f"[INIT] Broadcast address: {self.broadcast_ip}")

    # ---------- Setup ----------
    def _setup_socket(self):
        """Create and configure UDP socket for broadcast and unicast."""
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('', PORT))
        self.sock.settimeout(1.0)  # Non-blocking with timeout

    def _get_local_ip(self):
        """Return the local IP address of this host."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            local_ip = s.getsockname()[0]
            s.close()
            return local_ip
        except Exception:
            return "127.0.0.1"
    
    def _get_broadcast_ip(self):
        """Calculate broadcast address based on local IP and subnet mask."""
        try:
            ip_parts = self.ip.split('.')
            broadcast = f"{ip_parts[0]}.{ip_parts[1]}.255.255"
            return broadcast
        except Exception:
            return "255.255.255.255"

    # ---------- Message Handling ----------
    def _next_seq(self):
        """Increment and return the next sequence number."""
        with self.lock:
            self.seq += 1
            return self.seq

    def _calculate_crc32(self, data: str) -> str:
        """Calculate CRC32 checksum for data."""
        crc = binascii.crc32(data.encode()) & 0xffffffff
        return f"{crc:08x}"

    def _make_packet(self, msgtype: str, body: str) -> str:
        """Build packet string according to format spec."""
        timestamp = int(time.time() * 1000)
        seq = self._next_seq()
        header = f"[{msgtype}]|{self.id}|{seq}|{timestamp}|0|3"
        packet_without_crc = f"{header}|{body}"
        crc = self._calculate_crc32(packet_without_crc)
        return f"{packet_without_crc}|{crc}"

    def _verify_crc32(self, packet: str) -> bool:
        """Verify CRC32 checksum of received packet."""
        try:
            parts = packet.rsplit('|', 1)
            if len(parts) != 2:
                return False
            data, received_crc = parts
            calculated_crc = self._calculate_crc32(data)
            return calculated_crc.lower() == received_crc.lower()
        except Exception as e:
            # print(f"[ERROR] CRC verification failed: {e}") COMMENTED OUT
            return False

    def _send(self, data: str, addr: tuple):
        """Send encoded UDP packet to destination."""
        try:
            self.sock.sendto(data.encode(), addr)
        except Exception as e:
            print(f"[ERROR] Failed to send to {addr}: {e}")

    # ---------- Overlay Operations ----------
    def broadcast_sync(self):
        """Broadcast [PEER_SYNC] message to announce presence."""
        body = f"{self.ip},{self.port}"
        packet = self._make_packet("PEER_SYNC", body)
        # print(f"[SYNC] Broadcasting: {packet}") COMMENTED OUT
        self._send(packet, (self.broadcast_ip, PORT))
        
        with self.lock:
            self.metrics["syncs_sent"] += 1

    def send_ping(self, peer_id: str, peer_ip: str, peer_port: int):
        """Send [PING] message to a specific peer."""
        timestamp = int(time.time() * 1000)
        body = f"{self.ip},{timestamp}"
        packet = self._make_packet("PING", body)
        
        peer_addr = (peer_ip, peer_port)
        self._send(packet, peer_addr)
        #  print(f"[PING] Sent to {peer_id} at {peer_ip}")  COMMENTED OUT
        
        with self.lock:
            self.ping_sent[peer_id] = timestamp
            self.metrics["pings_sent"] += 1

    def send_pong(self, addr: tuple, ping_timestamp: int, sender_id: str):
        """Send [PONG] message back to sender."""
        body = f"{self.ip},ok,{ping_timestamp}"
        packet = self._make_packet("PONG", body)
        self._send(packet, addr)
        #  print(f"[PONG] Sent to {sender_id}")  COMMENTED OUT

    # ---------- File Transfer Operations ----------
    def announce_model_meta(self, ver: str, size: int, chunks: int, sha: str):
        """Announce metadata about a model delta to all peers."""
        body = f"ver={ver};size={size};chunks={chunks};sha256={sha}"
        pkt = self._make_packet("MODEL_META", body)
        self._send(pkt, (self.broadcast_ip, PORT))
        print(f"[META] Announced {ver}: {size} bytes → {chunks} chunks, SHA={sha}")

    def send_file_to_peer(self, peer_id: str, ver: str, filepath: str):
        """Send a fragmented file to a specific peer."""
        with self.lock:
            if peer_id not in self.peers:
                print(f"[ERROR] Peer {peer_id} not found")
                return False
            
            peer_info = self.peers[peer_id]
            peer_addr = (peer_info["ip"], peer_info["port"])
        
        print(f"[TRANSFER] Sending {ver} to {peer_id} at {peer_addr[0]}")
        self._fragment_and_send(ver, filepath, peer_addr)
        return True

    def broadcast_file_to_all_peers(self, ver: str, filepath: str):
        """Send a fragmented file to all discovered peers."""
        with self.lock:
            peer_list = list(self.peers.items())
        
        if not peer_list:
            print(f"[WARN] No peers available for transfer")
            return 0
        
        count = 0
        for peer_id, peer_info in peer_list:
            peer_addr = (peer_info["ip"], peer_info["port"])
            print(f"[TRANSFER] Sending {ver} to {peer_id} at {peer_addr[0]}")
            self._fragment_and_send(ver, filepath, peer_addr)
            count += 1
            time.sleep(0.5)  # Small delay between peers to avoid overwhelming network
        
        return count

    def _fragment_and_send(self, ver: str, filepath: str, addr: Tuple[str, int]):
        """Fragment and send file in ≤ MAX_UDP-sized base64 chunks."""
        with open(filepath, "rb") as f:
            data = f.read()
        
        total_chunks = (len(data) + MAX_UDP - 1) // MAX_UDP
        
        for i in range(total_chunks):
            start_idx = i * MAX_UDP
            end_idx = min((i + 1) * MAX_UDP, len(data))
            chunk = data[start_idx:end_idx]
            
            b64_chunk = base64.b64encode(chunk).decode('utf-8')
            body = f"ver={ver};idx={i};total={total_chunks};b64={b64_chunk}"
            pkt = self._make_packet("MODEL_CHUNK", body)
            
            self._send(pkt, addr)
            time.sleep(0.1)   # 20 ms pause between chunks
            
            with self.lock:
                self.metrics["chunks_sent"] += 1
            
                print(f"[SEND] TYPE=MODEL_CHUNK SRC={self.id} IDX={i+1}/{total_chunks} → {addr[0]}")

    def register_transfer_callback(self, version: str, callback):
        """Register a callback to be called when a transfer completes."""
        with self.lock:
            self._transfer_callbacks[version] = callback

    # ---------- Message Handlers ----------
    def handle_message(self, msg: str, addr: tuple):
        """Parse and process incoming UDP packet."""
        try:
            if not self._verify_crc32(msg):
                # print(f"[ERROR] Invalid CRC from {addr}") COMMENTED OUT
                return
            
            msg_without_crc = msg.rsplit('|', 1)[0]
            parts = msg_without_crc.split('|', 6)
            
            if len(parts) < 7:
                print(f"[ERROR] Malformed packet from {addr}")
                return
            
            msgtype = parts[0].strip('[]')
            sender_id = parts[1]
            body = parts[6]
            
            if sender_id == self.id:
                return
            
            if msgtype == "PEER_SYNC":
                self._handle_peer_sync(sender_id, body, addr)
            elif msgtype == "PING":
                self._handle_ping(sender_id, body, addr)
            elif msgtype == "PONG":
                self._handle_pong(sender_id, body)
            elif msgtype == "MODEL_META":
                self._handle_model_meta(sender_id, body)
            elif msgtype == "MODEL_CHUNK":
                self._handle_model_chunk(sender_id, body, addr)
            else:
                print(f"[WARN] Unknown message type: {msgtype}")
                
        except Exception as e:
            print(f"[ERROR] Failed to handle message from {addr}: {e}")

    def _handle_peer_sync(self, sender_id: str, body: str, addr: tuple):
        """Handle PEER_SYNC message."""
        try:
            parts = body.split(',')
            if len(parts) < 2:
                print(f"[ERROR] Invalid PEER_SYNC body: {body}")
                return
                
            ip = parts[0]
            port = int(parts[1])
            
            with self.lock:
                is_new = sender_id not in self.peers
                
                self.peers[sender_id] = {
                    "ip": ip,
                    "port": port,
                    "last_seen": time.time(),
                    "status": "active"
                }
                
                self.metrics["syncs_received"] += 1
                
                if is_new:
                    print(f"[SYNC] Added {sender_id} ({ip})")
                    
        except Exception as e:
            print(f"[ERROR] Failed to parse PEER_SYNC: {e}")

    def _handle_ping(self, sender_id: str, body: str, addr: tuple):
        """Handle PING message."""
        try:
            parts = body.split(',')
            if len(parts) < 2:
                print(f"[ERROR] Invalid PING body: {body}")
                return
                
            ping_timestamp = int(parts[1])
            
            with self.lock:
                if sender_id in self.peers:
                    self.peers[sender_id]["last_seen"] = time.time()
            
            self.send_pong(addr, ping_timestamp, sender_id)
            
        except Exception as e:
            print(f"[ERROR] Failed to handle PING: {e}")

    def _handle_pong(self, sender_id: str, body: str):
        """Handle PONG message."""
        try:
            parts = body.split(',')
            if len(parts) < 3:
                print(f"[ERROR] Invalid PONG body: {body}")
                return
            
            original_ping_timestamp = int(parts[2])
            current_time = int(time.time() * 1000)
            rtt = current_time - original_ping_timestamp
            
            if rtt < 0 or rtt > 10000:
                print(f"[WARN] Invalid RTT {rtt}ms from {sender_id}, ignoring")
                return
            
            with self.lock:
                if sender_id in self.peers:
                    self.peers[sender_id]["last_seen"] = time.time()
                
                if sender_id in self.ping_sent:
                    del self.ping_sent[sender_id]
                    self.metrics["pongs_received"] += 1
                    self.metrics["rtt_samples"].append(rtt)
                    
        except Exception as e:
            print(f"[ERROR] Failed to handle PONG: {e}")

    def _handle_model_meta(self, sender_id: str, body: str):
        """Handle MODEL_META announcement."""
        try:
            fields = {}
            for field in body.split(';'):
                if '=' in field:
                    key, value = field.split('=', 1)
                    fields[key] = value
            
            version = fields.get('ver', '')
            size = int(fields.get('size', 0))
            chunks = int(fields.get('chunks', 0))
            sha256 = fields.get('sha256', '')
            
            print(f"[META] {sender_id} announces {version}: {size} bytes, {chunks} chunks")
            
            with self.lock:
                if version not in self._model_buffers:
                    self._model_buffers[version] = {
                        "total": chunks,
                        "parts": {},
                        "sha256": sha256,
                        "sender": sender_id
                    }
                    
        except Exception as e:
            print(f"[ERROR] Failed to handle MODEL_META: {e}")

    def _handle_model_chunk(self, sender_id: str, body: str, addr: tuple):
        """Handle incoming MODEL_CHUNK messages and perform reassembly."""
        try:
            fields = {}
            for field in body.split(';'):
                if '=' in field:
                    key, value = field.split('=', 1)
                    fields[key] = value
            
            version = fields.get('ver', '')
            idx = int(fields.get('idx', -1))
            total = int(fields.get('total', 0))
            b64_data = fields.get('b64', '')

            print(f"[RECV] TYPE=MODEL_CHUNK VER={version} IDX={idx+1}/{total}")
            
            if not version or idx < 0 or total <= 0 or not b64_data:
                return
            
            print(f"[DEBUG] Entering _handle_model_chunk for {sender_id}, fields={body[:60]}...")

            with self.lock:
                if version not in self._model_buffers:
                    self._model_buffers[version] = {
                        "total": total,
                        "parts": {},
                        "sha256": None,
                        "sender": sender_id
                    }
                
                else:
    # Preserve total and sha256 that came from META
                    existing = self._model_buffers[version]
                    existing["total"] = existing.get("total", total)

                self._model_buffers[version]["parts"][idx] = b64_data
                self.metrics["chunks_received"] += 1
                
                buffer = self._model_buffers[version]
                received_chunks = len(buffer["parts"])

                print(f"[DEBUG] {version}: total={buffer.get('total')}  received={received_chunks}")
                
                if received_chunks % 10 == 0 or received_chunks == buffer["total"]:
                    print(f"[RECV] {version} progress: {received_chunks}/{buffer['total']} chunks")
                
                # Check if transfer is complete
                is_complete = received_chunks == buffer["total"]
            
            # Reassemble outside of lock to avoid blocking other operations
            if is_complete:
                print(f"[DEBUG] Trigger condition met for {version}")
            if is_complete:
                print(f"[DEBUG] All {received_chunks}/{buffer['total']} chunks received for {version}, starting reassembly thread")
                # Use a thread to handle reassembly so we don't block the listener
                threading.Thread(
                    target=self._reassemble_and_verify,
                    args=(version,),
                    daemon=True,
                    name=f"Reassemble-{version}"
                ).start()

                    
        except Exception as e:
            print(f"[ERROR] Failed to handle MODEL_CHUNK: {e}")

    def _reassemble_and_verify(self, version: str):
        print(f"[DEBUG] reassemble_and_verify triggered for {version}")
        """Reassemble complete file and verify integrity."""
        try:
            # Copy buffer data while holding lock briefly
            with self.lock:
                if version not in self._model_buffers:
                    return
                buffer = dict(self._model_buffers[version])
            
            print(f"[REASSEMBLE] Starting reassembly for {version}...")
            
            reassembled_data = b''
            
            for i in range(buffer["total"]):
                if i not in buffer["parts"]:
                    print(f"[ERROR] Missing chunk {i} for {version}")
                    return
                
                chunk_b64 = buffer["parts"][i]
                chunk_bytes = base64.b64decode(chunk_b64)
                reassembled_data += chunk_bytes
            
            computed_sha = hashlib.sha256(reassembled_data).hexdigest()
            expected_sha = buffer.get("sha256", "")
            
            with self.lock:
                self.metrics["transfers_received"] += 1
            
            if expected_sha:
                if computed_sha == expected_sha:
                    print(f"[OK]  Transfer complete: {version} ({len(reassembled_data)} bytes)")
                    print(f"[OK]  SHA256 verified: {computed_sha}")
                    print(f"[OK] SHA verified for {version}")
                    print(f"[TEST DONE] ✔ All fragments received and verified")
                else:
                    print(f"[ERROR] SHA mismatch for {version}")
                    print(f"  Expected: {expected_sha}")
                    print(f"  Computed: {computed_sha}")
                    return
            else:
                print(f"[OK] Transfer complete: {version} ({len(reassembled_data)} bytes)")
                print(f"[OK] SHA256: {computed_sha}")
            
            # Call callback if registered (outside of lock)
            callback = None
            with self.lock:
                callback = self._transfer_callbacks.get(version)
            
            if callback:
                try:
                    callback(version, reassembled_data, computed_sha)
                except Exception as e:
                    print(f"[ERROR] Callback failed for {version}: {e}")
            
            print(f"[REASSEMBLE] Completed for {version}, listener continuing normally")
                
        except Exception as e:
            print(f"[ERROR] Failed to reassemble {version}: {e}")

    # ---------- Thread Tasks ----------
    def listener(self):
        """Continuously listen for incoming packets."""
        print(f"[START] Listener thread started")
        while self.running:
            try:
                data, addr = self.sock.recvfrom(65535)  # Increased for large chunks
                msg = data.decode('utf-8')
                self.handle_message(msg, addr)
            except socket.timeout:
                continue
            except UnicodeDecodeError:
                print(f"[ERROR] Failed to decode packet from {addr}: {e}")
            except Exception as e:
                if self.running:
                    print(f"[ERROR] Listener error: {e}")

    def broadcaster(self):
        """Periodically broadcast PEER_SYNC messages."""
        print(f"[START] Broadcaster thread started")
        while self.running:
            self.broadcast_sync()
            time.sleep(SYNC_INTERVAL)

    def heartbeat(self):
        """Send pings and remove inactive peers."""
        print(f"[START] Heartbeat thread started")
        while self.running:
            time.sleep(PING_INTERVAL)
            
            current_time = time.time()
            peers_to_remove = []
            peers_to_ping = []
            
            with self.lock:
                for peer_id, peer_info in list(self.peers.items()):
                    time_since_seen = current_time - peer_info["last_seen"]
                    
                    if time_since_seen > REMOVE_TIMEOUT:
                        peers_to_remove.append(peer_id)
                    else:
                        peers_to_ping.append({
                            "id": peer_id,
                            "ip": peer_info["ip"],
                            "port": peer_info["port"]
                        })
                
                for peer_id in peers_to_remove:
                    print(f"[DROP] {peer_id} removed (timeout)")
                    del self.peers[peer_id]
                    if peer_id in self.ping_sent:
                        del self.ping_sent[peer_id]
            
            for peer in peers_to_ping:
                self.send_ping(peer["id"], peer["ip"], peer["port"])

    def summary(self):
        """Print peer-table summary and metrics periodically."""
        print(f"[START] Summary thread started")
        while self.running:
            time.sleep(10)
            
            with self.lock:
                peers_snapshot = dict(self.peers)
                metrics_snapshot = dict(self.metrics)
                rtt_samples = list(self.metrics["rtt_samples"])
            
            active_count = len(peers_snapshot)
            pings_sent = metrics_snapshot["pings_sent"]
            pongs_received = metrics_snapshot["pongs_received"]
            reliability = (pongs_received / pings_sent * 100) if pings_sent > 0 else 0
            mean_rtt = (sum(rtt_samples) / len(rtt_samples)) if rtt_samples else 0
            
            print(f"\n{'='*60}")
            print(f"[TABLE] {active_count} active peer{'s' if active_count != 1 else ''}")
            if active_count > 0:
                for peer_id, info in peers_snapshot.items():
                    age = int(time.time() - info["last_seen"])
                    print(f"  - {peer_id}: {info['ip']} (seen {age}s ago)")
            
            print(f"\n[METRICS] Network Statistics:")
            print(f"  - Pings sent: {pings_sent}, Pongs received: {pongs_received}")
            print(f"  - Reliability: {reliability:.1f}%")
            
            if len(rtt_samples) > 0:
                print(f"  - Mean RTT: {mean_rtt:.1f} ms (min: {min(rtt_samples):.1f}, max: {max(rtt_samples):.1f})")
            
            print(f"\n[METRICS] Transfer Statistics:")
            print(f"  - Chunks sent: {metrics_snapshot['chunks_sent']}")
            print(f"  - Chunks received: {metrics_snapshot['chunks_received']}")
            print(f"  - Transfers received: {metrics_snapshot['transfers_received']}")
            print(f"{'='*60}\n")

    def test_sender(self, interval: int = 30, file_size: int = 300000):
        """Periodically send test files to all peers."""
        print(f"[TEST] Test sender thread started (interval: {interval}s, size: {file_size} bytes)")
        
        # Wait for initial peer discovery
        print(f"[TEST] Waiting 15 seconds for initial peer discovery...")
        time.sleep(15)
        
        send_count = 0
        while self.running:
            send_count += 1
            peers = self.get_active_peers()
            
            if peers:
                print(f"\n{'='*60}")
                print(f"[TEST] Send cycle #{send_count}")
                print(f"[TEST] Found {len(peers)} peer(s): {peers}")
                
                # Create test file
                path, sha, size = self.create_test_file(file_size)
                version = f"test_v{int(time.time())}"
                total_chunks = (size + MAX_UDP - 1) // MAX_UDP
                
                print(f"[TEST] Created test file: {version} ({size} bytes, {total_chunks} chunks)")
                print(f"[TEST] SHA256: {sha}")
                print(f"[INIT] Created delta {version} ({size} bytes, {total_chunks} chunks)")
                
                # Announce metadata
                self.announce_model_meta(version, size, total_chunks, sha)
                time.sleep(1)
                
                # Broadcast to all peers
                count = self.broadcast_file_to_all_peers(version, path)
                print(f"[TEST] File sent to {count} peer(s)")
                print(f"[TEST] Next send in {interval} seconds...")
                print(f"{'='*60}\n")
                
                # Cleanup
                try:
                    os.unlink(path)
                except Exception as e:
                    print(f"[WARN] Failed to cleanup temp file: {e}")
            else:
                print(f"[TEST] Send cycle #{send_count}: No peers found, waiting...")
            
            # Wait for next interval (break into smaller sleeps to respond to shutdown faster)
            for i in range(interval):
                if not self.running:
                    break
                time.sleep(1)
        
        print(f"[TEST] Test sender thread stopped")

    # ---------- Control ----------
    def start(self, enable_test_sender: bool = False, test_interval: int = 30, test_file_size: int = 300000):
        """Start all background threads."""
        self.running = True
        
        threads = [
            threading.Thread(target=self.listener, daemon=True, name="Listener"),
            threading.Thread(target=self.broadcaster, daemon=True, name="Broadcaster"),
            threading.Thread(target=self.heartbeat, daemon=True, name="Heartbeat"),
            # threading.Thread(target=self.summary, daemon=True, name="Summary")  COMMENTED OUT
        ]
        
        # Add test sender thread if enabled
        if enable_test_sender:
            threads.append(
                threading.Thread(
                    target=self.test_sender, 
                    args=(test_interval, test_file_size),
                    daemon=True, 
                    name="TestSender"
                )
            )
        
        for thread in threads:
            thread.start()
        
        print(f"[START] Node {self.id} running")
        print(f"[INFO] Discovery: {SYNC_INTERVAL}s, Ping: {PING_INTERVAL}s, Timeout: {REMOVE_TIMEOUT}s")
        if enable_test_sender:
            print(f"[INFO] Test sender enabled: {test_interval}s interval, {test_file_size} byte files")
        print()

    def stop(self):
        """Stop all threads and close socket."""
        print(f"\n[STOP] Shutting down node {self.id}")
        self.running = False
        time.sleep(2)
        
        if self.sock:
            self.sock.close()
        
        print(f"[STOP] Node {self.id} stopped cleanly")

    # ---------- Utility Methods ----------
    def get_active_peers(self):
        """Return list of active peer IDs."""
        with self.lock:
            return list(self.peers.keys())

    def create_test_file(self, size_bytes: int = 300000) -> Tuple[str, str, int]:
        """Create a temporary binary file for testing."""
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
        data = os.urandom(size_bytes)
        tmp.write(data)
        tmp.close()
        sha = hashlib.sha256(data).hexdigest()
        return tmp.name, sha, size_bytes


# ---------- Entry Point ----------
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 unified_overlay_transfer.py <node_id> [--send-test] [--interval SECONDS] [--size BYTES]")
        print("Example: python3 unified_overlay_transfer.py Pi-1")
        print("         python3 unified_overlay_transfer.py Pi-2 --send-test")
        print("         python3 unified_overlay_transfer.py Pi-3 --send-test --interval 60 --size 500000")
        sys.exit(1)
    
    node_id = sys.argv[1]
    send_test = "--send-test" in sys.argv
    
    # Parse optional arguments
    test_interval = 30  # Default 30 seconds
    test_file_size = 300000  # Default 300KB
    
    if "--interval" in sys.argv:
        try:
            idx = sys.argv.index("--interval")
            test_interval = int(sys.argv[idx + 1])
        except (IndexError, ValueError):
            print("[WARN] Invalid --interval value, using default 30 seconds")
    
    if "--size" in sys.argv:
        try:
            idx = sys.argv.index("--size")
            test_file_size = int(sys.argv[idx + 1])
        except (IndexError, ValueError):
            print("[WARN] Invalid --size value, using default 300000 bytes")
    
    node = PeerNode(node_id)
    node.start(enable_test_sender=send_test, test_interval=test_interval, test_file_size=test_file_size)
    
    try:
        print("\n[INFO] Node running. Press Ctrl+C to stop.\n")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        node.stop()
        print("\n[EXIT] Node stopped.")
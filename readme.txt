Final Project instructions before running:

1. Use ipconfig or hostname -I to make sure both devices are on the same network-- update broadcast address

2. Make sure firewall isn't blocking traffic
 - Windows activation: netsh advfirewall firewall add rule name="UDP5000 Allow" dir=in action=allow protocol=UDP localport=5000
 - Windows deactivation netsh advfirewall firewall delete rule name="UDP5000 Allow"
 
3. Ensure network is private: 
 - Go to Settings > Network & Internet > Wi‑Fi > Properties
 - Under Network profile type, choose Private

4.  python3 FINAL.py <NODE_ID> [--send-test] [--interval SECONDS] [--size BYTES]
    python FINAL.py Sender --send-test

5. 
If you want to save the file on the receiver’s PC
Add a simple write step inside _reassemble_and_verify once the transfer is verified, for example:

python
output_dir = os.path.join(os.getcwd(), "received_files")
os.makedirs(output_dir, exist_ok=True)
output_path = os.path.join(output_dir, f"{version}.bin")

with open(output_path, "wb") as f:
    f.write(reassembled_data)

print(f"[SAVE] File saved successfully to: {output_path}")
Place this right after the successful SHA verification block (where [OK] SHA verified for ... is printed).

This way:

Every fully verified transfer gets permanently written to disk.
The receiver’s machine gets a file per version under a folder like received_files/.


This creates a random 300 KB file, splits it into 7 chunks under 45 KB each,
Base64‑encodes them, and sends each as a UDP MODEL_CHUNK message.





WHAT ASSIGNMENT 7 DOES:
The receiver saw all seven chunks, reassembled them in order, computed a SHA‑256 digest, 
and verified it matches the original file — that confirms zero data loss through the fragmentation layer.



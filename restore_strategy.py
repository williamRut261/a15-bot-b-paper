"""Restore unchanged original A15 JSON from compressed repository source.

Does not access networks, accounts, or brokers.
"""
from __future__ import annotations
import base64
import gzip
import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SHA256 = "90f59e12ea4bb409c0e93f2fcb39fe750fef8bf1115708abeaa7276a1f2345d5"

def ensure_strategy() -> Path:
    path = ROOT / "original_a15.json"
    if path.exists():
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != SHA256:
            raise ValueError("A15 source file does not match the original verified SHA256")
        return path
    encoded = (ROOT / "original_a15.json.gz.b64").read_text()
    raw = gzip.decompress(base64.b64decode(encoded))
    if hashlib.sha256(raw).hexdigest() != SHA256:
        raise ValueError("Compressed A15 source failed SHA256 verification")
    path.write_bytes(raw)
    return path

if __name__ == "__main__":
    print("A15 source restored:", ensure_strategy())
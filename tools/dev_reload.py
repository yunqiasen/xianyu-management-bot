"""Reload the whole GuDong process, including CookieManager, not just ASGI."""
import hashlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

IGNORED = {'.git', '.venv', '__pycache__', 'node_modules', 'data', 'logs', 'backups', 'uploads', 'tests', '.pytest_cache', 'browser_data', 'trajectory_history'}

def snapshot(root):
    result = {}
    for directory, folders, files in os.walk(root):
        folders[:] = [name for name in folders if name not in IGNORED]
        for name in files:
            path = Path(directory)/name
            if path.suffix not in {'.py', '.yml', '.yaml'}:
                continue
            try:
                result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).digest()
            except FileNotFoundError:
                pass
    return result

def stop(child):
    # Process group includes browser children, even if Start.py already exited.
    try: os.killpg(child.pid, signal.SIGTERM)
    except ProcessLookupError: pass
    try: child.wait(timeout=15)
    except subprocess.TimeoutExpired: pass
    try: os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError: pass
    child.wait()

def main():
    root=Path(__file__).resolve().parents[1]
    running=True
    def shutdown(*_):
        nonlocal running
        running=False
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    child=None
    try:
        while running:
            before=snapshot(root)
            child=subprocess.Popen([sys.executable, 'Start.py'], cwd=root, start_new_session=True)
            print(f'[dev-reload] started pid={child.pid}', flush=True)
            while running and child.poll() is None:
                time.sleep(1)
                after=snapshot(root)
                if after != before:
                    # Wait until editor writes settle.
                    while running:
                        time.sleep(1)
                        stable=snapshot(root)
                        if stable == after: break
                        after=stable
                    print('[dev-reload] source changed; restarting whole application', flush=True)
                    break
            crashed = child.poll() is not None
            stop(child); child=None
            if crashed and running:
                raise SystemExit(1)
            if running: time.sleep(1)
    finally:
        if child is not None: stop(child)
if __name__=='__main__': main()

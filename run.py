"""Container entrypoint: web UI (gunicorn) + MCP server (streamable-http) in one container."""
import os
import signal
import subprocess
import sys
import time

PORT = os.environ.get("PORT", "5000")
MCP_PORT = os.environ.get("MCP_PORT", "8001")


def main() -> int:
    env = dict(os.environ, MCP_TRANSPORT="streamable-http", MCP_PORT=MCP_PORT)
    procs = [
        subprocess.Popen([sys.executable, "-m", "gunicorn", "-b", f"0.0.0.0:{PORT}",
                          "--workers", "2", "--threads", "4", "--timeout", "600", "app:app"], env=env),
        subprocess.Popen([sys.executable, "mcp_server.py", "streamable-http"], env=env),
    ]

    def stop(*_):
        for p in procs:
            if p.poll() is None:
                p.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while all(p.poll() is None for p in procs):
        time.sleep(1)
    stop()
    for p in procs:
        p.wait()
    return max(p.returncode or 0 for p in procs)


if __name__ == "__main__":
    sys.exit(main())

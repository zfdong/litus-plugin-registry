#!/usr/bin/env python3
"""Install registry plugins with a real litus-cli, as a user would.

    python scripts/smoke.py --cli path/to/litus-cli                 # every plugin
    python scripts/smoke.py --cli path/to/litus-cli hello-litus     # only these

Builds the registry into a temporary site, serves it on 127.0.0.1, and with an empty plugins
folder runs `litus-cli plugins browse` and `litus-cli plugins get <id>` for each plugin, so the
plugin goes through Litus's own checks: the hash, the manifest, the module. A process plugin
with a Python environment downloads Python and its packages, which takes a while.

Process plugins aren't sandboxed, and installing one doesn't run it, but run this only on
plugins you have read.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("ids", nargs="*", help="plugins to install (default: all)")
    ap.add_argument("--cli", required=True, type=Path, help="the litus-cli program")
    args = ap.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        site, plugins = Path(tmp) / "site", Path(tmp) / "plugins"
        try:
            entries = build.run(None, site, None)
        except build.BuildError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        ids = args.ids or [e["id"] for e in entries]
        unknown = sorted(set(ids) - {e["id"] for e in entries})
        if unknown:
            print(f"ERROR: not in the registry: {unknown}", file=sys.stderr)
            return 1

        handler = functools.partial(QuietHandler, directory=str(site))
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/api/plugins/registry.json"
        env = dict(os.environ, LITUS_PLUGINS_DIR=str(plugins))

        def cli(*a: str) -> str:
            r = subprocess.run([str(args.cli), *a], env=env, capture_output=True, text=True,
                               timeout=1800)
            if r.returncode != 0:
                raise RuntimeError(f"litus-cli {' '.join(a)} failed:\n{r.stdout}{r.stderr}")
            return r.stdout

        failed = 0
        try:
            listed = cli("plugins", "browse", url)
            for pid in ids:
                try:
                    if not any(line.split()[:1] == [pid] for line in listed.splitlines()):
                        raise RuntimeError("`plugins browse` doesn't list it")
                    cli("plugins", "get", pid, url)
                    print(f"OK    {pid}")
                except RuntimeError as exc:
                    failed += 1
                    print(f"FAIL  {pid}: {exc}")
        except RuntimeError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        finally:
            server.shutdown()
    return 1 if failed else 0


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


if __name__ == "__main__":
    sys.exit(main())

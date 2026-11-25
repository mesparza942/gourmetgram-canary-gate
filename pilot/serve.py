"""Run an isolated copy of upstream GourmetGram, with an optional fixed prediction delay."""

import argparse
import logging
import os
import runpy
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app_directory", type=Path, help="working copy containing app.py and food11.pth")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--delay-ms", type=int, default=0)
    args = parser.parse_args()
    if args.delay_ms < 0:
        parser.error("delay must be nonnegative")

    import torch
    from flask import request
    from werkzeug.serving import make_server

    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)
    os.chdir(args.app_directory)
    app = runpy.run_path(str(Path("app.py").resolve()))["app"]

    if args.delay_ms:
        @app.before_request
        def delay_prediction():
            if request.method == "POST" and request.path == "/predict":
                time.sleep(args.delay_ms / 1000)

    logging.getLogger("werkzeug").setLevel(logging.ERROR)
    server = make_server("127.0.0.1", args.port, app, threaded=True)
    print(f"Local GourmetGram ready on port {server.port}, delay {args.delay_ms} ms", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

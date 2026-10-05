"""Private line protocol worker; stdout contains only response envelopes."""

import argparse
import contextlib
import io
import json
import logging
import os
import sys
from pathlib import Path

import rent_adapter as adapter

parser = argparse.ArgumentParser()
parser.add_argument("--state", required=True)
parser.add_argument("--proxy", required=True)
parser.add_argument("--max-bytes", type=int, required=True)
args = parser.parse_args()
os.umask(0o077)
state = Path(args.state)
state.mkdir(parents=True, exist_ok=True)
private = state / "private-diagnostics"
private.mkdir(exist_ok=True)
adapter.upstream.DEBUG_RESPONSE_DIR = private
adapter.upstream.MAX_QRATOR_RETRIES_PER_REQUEST = 1
adapter.upstream.MAX_TRANSIENT_GET_RETRIES = 1
logging.basicConfig(level=logging.CRITICAL)
session = adapter.upstream.requests.Session(
    impersonate=adapter.upstream.HTTP_IMPERSONATE_PROFILE, trust_env=False, proxy=args.proxy
)
try:
    for line in sys.stdin:
        try:
            request = json.loads(line)
            adapter.upstream.REFERER = request["referer"]
            chain = []
            with contextlib.redirect_stdout(io.StringIO()):
                response = adapter.fetch(
                    session,
                    request["url"],
                    headers=adapter.upstream.DOCUMENT_REQUEST_HEADERS,
                    referer=request["referer"],
                    chain=chain,
                )
            text = response.text
            if len(text.encode("utf-8")) > args.max_bytes:
                raise RuntimeError("Response exceeded configured limit")
            result = {
                "ok": True,
                "status": response.status_code,
                "url": response.url or request["url"],
                "html": text,
                "verification_chain": chain,
            }
        except Exception as exc:
            (private / "last-error.txt").write_text(str(exc), encoding="utf-8")
            result = {"ok": False, "error_type": type(exc).__name__}
        print(json.dumps(result, ensure_ascii=False), flush=True)
        # Keep the newest diagnostics; they may contain active challenge tokens.
        files = sorted(private.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
        for path in files[12:]:
            if path.is_file():
                path.unlink()
finally:
    session.close()

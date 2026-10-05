"""Bounded search/detail probe using the upstream protection flow."""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import os
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote, urljoin, urlsplit

sys.path.insert(
    0,
    os.environ.get(
        "RENT_MONITOR_AVITO_PARSER_ROOT", str(Path(__file__).resolve().parent / "vendor")
    ),
)
import main as upstream


def avito_url(value: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or parts.hostname not in {"www.avito.ru", "avito.ru"}
        or parts.username
        or parts.password
    ):
        raise ValueError("Expected an HTTPS URL on avito.ru")
    return value


def fetch(session, url, *, headers, referer, chain, limit=5):
    """Retry the exact URL in the same session after verification."""
    url = avito_url(url)
    redirects = 0
    failures = 0
    upstream.PAGE_REQUEST_HEADERS = {**upstream.PAGE_REQUEST_HEADERS, "Referer": referer}
    for attempt in range(limit + 1):
        response = upstream.get_with_qrator_recovery(
            session,
            url,
            session_headers={**headers, "Referer": referer},
            timeout_seconds=20,
            context="adapter request",
            stream_response_body=True,
            verification_chain=chain,
        )
        print(
            json.dumps(
                {"event": "response", "status": response.status_code, "attempt": attempt + 1}
            ),
            flush=True,
        )
        if response.status_code == 200:
            return response
        upstream.save_response_body(response, context=f"adapter-protected-{attempt}")
        print(
            json.dumps(
                {
                    "event": "protection-format",
                    "content_type": response.headers.get("content-type", ""),
                    "pow_json": upstream.response_has_pow_challenge(response),
                    "captcha_dispatcher": upstream.is_firewall_captcha_dispatcher_response(
                        response
                    ),
                    "geetest_html": upstream.is_geetest_firewall_html(response.text),
                }
            ),
            flush=True,
        )
        if response.status_code in {301, 302, 303, 307, 308}:
            redirects += 1
            if redirects > 3:
                raise RuntimeError("Too many redirects")
            url = avito_url(urljoin(url, response.headers.get("location", "")))
            continue
        if attempt == limit:
            raise RuntimeError("Protection transition limit reached")
        try:
            if (
                response.status_code in {429, 439}
                and not upstream.response_has_pow_challenge(response)
                and "pow_challenge" in response.text
                and "/web/3/firewallPow/get" in response.text
            ):
                # The live document flow reads the challenge from a cookie,
                # whereas upstream only supports the items-XHR JSON response.
                challenges = [
                    cookie.value
                    for cookie in session.cookies.jar
                    if cookie.name == "pow_challenge"
                    and cookie.domain.lstrip(".") in {"avito.ru", "www.avito.ru"}
                ]
                if not challenges:
                    raise RuntimeError("HTML PoW has no Avito challenge cookie")
                challenge_response = SimpleNamespace(
                    json=lambda challenge=challenges[-1]: {"pow_challenge": unquote(challenge)}
                )
                result = upstream.run_pow_verification(session, challenge_response)
                # Match the live document script after verified=true.
                session.cookies.set("pow_solved", "1", domain=urlsplit(url).hostname, path="/")
                for cookie in session.cookies.jar:
                    if cookie.name == "pow_solved" and cookie.domain == urlsplit(url).hostname:
                        cookie.expires = int(time.time()) + 10
            else:
                result = upstream.handle_firewall_response(
                    session, response, context="adapter request"
                )
        except upstream.GeeTestSolveFailed:
            failures += 1
            if failures >= 2:
                raise RuntimeError("GeeTest rejected two fresh attempts") from None
            continue
        if isinstance(result, upstream.GeeTestVerified):
            chain.append("GeeTest")
        elif result is not None:
            chain.append("firewallPow")
        else:
            raise RuntimeError(f"Unresolved HTTP {response.status_code}")
    raise RuntimeError("Request attempt limit reached")


def run(args):
    sys.path.insert(0, str(Path(args.parser_root).resolve()))
    from rent_monitor.parsers.avito import parse_search_page
    from rent_monitor.parsers.avito_detail import parse_detail

    search_url = avito_url(Path(args.url_file).read_text(encoding="utf-8-sig").strip())
    output = Path(args.output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    private = output / "private-diagnostics"
    private.mkdir(exist_ok=True)
    if os.name != "nt":
        os.chmod(private, 0o700)
    upstream.DEBUG_RESPONSE_DIR = private
    upstream.MAX_QRATOR_RETRIES_PER_REQUEST = 1
    upstream.MAX_TRANSIENT_GET_RETRIES = 1
    upstream.CATALOG_PAGE_URL = search_url
    upstream.REFERER = search_url
    upstream.DOCUMENT_REQUEST_HEADERS = {
        **upstream.DOCUMENT_REQUEST_HEADERS,
        "Referer": "https://www.avito.ru/",
    }
    upstream.PAGE_REQUEST_HEADERS = {**upstream.PAGE_REQUEST_HEADERS, "Referer": search_url}
    report = {"upstream_commit": "2d09315", "cycles": [], "verification_chain": []}
    if hasattr(signal, "SIGALRM"):

        def deadline(signum, frame):
            raise TimeoutError("Probe exceeded 240 seconds")

        signal.signal(signal.SIGALRM, deadline)
        signal.alarm(240)
    session = upstream.requests.Session(
        impersonate=upstream.HTTP_IMPERSONATE_PROFILE, trust_env=False, proxy=args.proxy or None
    )
    try:
        for cycle in range(args.cycles):
            if cycle:
                time.sleep(args.pause)
            response = fetch(
                session,
                search_url,
                headers=upstream.DOCUMENT_REQUEST_HEADERS,
                referer="https://www.avito.ru/",
                chain=report["verification_chain"],
            )
            parsed = parse_search_page(
                response.text, response.url or search_url, observed_at=datetime.now(UTC)
            )
            record = {
                "cycle": cycle + 1,
                "search_status": response.status_code,
                "cards": len(parsed.candidates),
                "details": [],
            }
            report["cycles"].append(record)
            print(
                json.dumps(
                    {"event": "search", "cycle": cycle + 1, "cards": len(parsed.candidates)}
                ),
                flush=True,
            )
            if not parsed.candidates:
                raise RuntimeError("HTTP 200 contains no parsed listings")
            for candidate in parsed.candidates[: args.details]:
                time.sleep(args.pause)
                detail_record = {"id": candidate.source_id, "complete": False}
                record["details"].append(detail_record)
                response = fetch(
                    session,
                    candidate.url,
                    headers=upstream.DOCUMENT_REQUEST_HEADERS,
                    referer=search_url,
                    chain=report["verification_chain"],
                )
                detail_record["status"] = response.status_code
                details = parse_detail(
                    response.text, candidate.source_id, response.url or candidate.url
                )
                if not details:
                    raise RuntimeError("Detail has no verified listing ID and description")
                detail_record.update(
                    {
                        "complete": True,
                        "description_chars": len(details.get("description", "")),
                        "parameters": len(details.get("characteristics", {})),
                    }
                )
                listing = dataclasses.asdict(candidate)
                (output / f"listing-{candidate.source_id}.json").write_text(
                    json.dumps(
                        {"listing": listing, "details": details},
                        ensure_ascii=False,
                        indent=2,
                        default=str,
                    ),
                    encoding="utf-8",
                )
                print(json.dumps({"event": "detail", **detail_record}), flush=True)
        report["success"] = True
    except Exception as exc:
        # Detailed upstream exceptions may contain challenge tokens.
        report.update({"success": False, "error_type": type(exc).__name__})
        (private / "error.txt").write_text(str(exc), encoding="utf-8")
        print(json.dumps({"event": "failed", "error_type": type(exc).__name__}), flush=True)
    finally:
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)
        session.close()
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return 0 if report.get("success") else 1


def cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url-file", required=True)
    parser.add_argument("--parser-root", required=True)
    parser.add_argument("--proxy", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--details", type=int, choices=range(1, 4), default=1)
    parser.add_argument("--cycles", type=int, choices=range(1, 4), default=1)
    parser.add_argument("--pause", type=float, default=15)
    args = parser.parse_args()
    if args.pause < 5:
        parser.error("--pause must be at least 5 seconds")
    # Keep upstream diagnostics out of console; structured events above are public.
    logging.basicConfig(level=logging.CRITICAL)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(cli())

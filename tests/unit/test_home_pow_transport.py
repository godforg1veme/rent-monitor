import sys

import pytest

from rent_monitor.browser.home_pow import HomePowTransport, HomeRouteUnavailable


def make_transport(tmp_path, code, timeout=2):
    worker = tmp_path / "worker.py"
    worker.write_text(code)
    return HomePowTransport(
        tmp_path / "state",
        1024,
        python_path=sys.executable,
        worker_path=str(worker),
        proxy="http://127.0.0.1:18782",
        timeout_seconds=timeout,
    )


@pytest.mark.asyncio
async def test_session_survives_search_and_detail_requests(tmp_path):
    transport = make_transport(
        tmp_path,
        "import sys,json\nn=0\nfor line in sys.stdin:\n r=json.loads(line); n+=1\n"
        " print(json.dumps({'ok':True,'status':200,'url':r['url'],"
        "'html':str(n)+' '+r['referer']}),flush=True)\n",
    )
    try:
        first = await transport.fetch("https://www.avito.ru/moskva/kvartiry")
        second = await transport.fetch_listing(
            "123456789", "https://www.avito.ru/moskva/test_123456789"
        )
        assert first.html == "1 https://www.avito.ru/"
        assert second.html == "2 https://www.avito.ru/moskva/kvartiry"
        assert (await transport.current_page()) is second
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_deadline_kills_worker_and_next_request_recovers(tmp_path):
    flag = tmp_path / "once"
    code = (
        f"import sys,json,time,pathlib\np=pathlib.Path({str(flag)!r})\n"
        "for line in sys.stdin:\n r=json.loads(line)\n if not p.exists():\n"
        "  p.touch(); time.sleep(60)\n"
        " print(json.dumps({'ok':True,'status':200,'url':r['url'],"
        "'html':'recovered'}),flush=True)\n"
    )
    transport = make_transport(tmp_path, code, timeout=0.5)
    with pytest.raises(TimeoutError):
        await transport.fetch("https://www.avito.ru/test")
    assert transport._process is None
    try:
        assert (await transport.fetch("https://www.avito.ru/test")).html == "recovered"
    finally:
        await transport.aclose()


@pytest.mark.asyncio
async def test_external_url_never_starts_worker(tmp_path):
    transport = make_transport(tmp_path, "raise RuntimeError()")
    with pytest.raises(ValueError):
        await transport.fetch("https://example.com/")
    assert transport._process is None


@pytest.mark.asyncio
async def test_network_failure_is_retryable_and_drops_stale_session(tmp_path):
    transport = make_transport(
        tmp_path,
        "import sys,json\nfor line in sys.stdin:\n"
        " print(json.dumps({'ok':False,'error_type':'ProxyError'}),flush=True)\n",
    )
    with pytest.raises(HomeRouteUnavailable):
        await transport.fetch("https://www.avito.ru/test")
    assert transport._process is None


def test_home_offline_retries_without_hourly_backoff():
    from rent_monitor.core.source_state import SourceRunHealth, _failure_schedule

    assert _failure_schedule("home_route_unavailable", 100, None) == (SourceRunHealth.DEGRADED, 60)

import httpx
import pytest
import respx

from web_mcp.config import FetchCfg
from web_mcp.net.safe_http import BlockedError, FetchError, SafeHttp, ip_is_public


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "10.1.2.3", "192.168.0.5", "172.16.9.9", "169.254.169.254", "100.64.0.1", "0.0.0.0",
    "::1", "fe80::1", "fd00:ec2::254", "fc00::1", "::ffff:127.0.0.1", "::ffff:10.0.0.1", "224.0.0.1",
    "64:ff9b::7f00:1", "2002:7f00:0001::1", "not-an-ip",
])
def test_non_public_ips_blocked(ip):
    assert not ip_is_public(ip)


@pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:2800:220:1:248:1893:25c8:1946"])
def test_public_ips_allowed(ip):
    assert ip_is_public(ip)


def make(resolver_map):
    async def resolver(host, port):
        return resolver_map.get(host, ["93.184.216.34"])

    return SafeHttp(FetchCfg(), resolver=resolver)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)", "data:text/plain,hi",
    "http://user:pw@example.com/", "http://example.com:22/", "http:///nohost",
])
async def test_bad_urls_blocked(url):
    with pytest.raises(BlockedError):
        await make({}).validate(url)


@pytest.mark.parametrize("host", ["localhost", "internal.corp", "metadata"])
async def test_hostname_resolving_private_blocked(host):
    h = make({host: ["127.0.0.1"], "mixed.example": ["93.184.216.34", "10.0.0.1"]})
    with pytest.raises(BlockedError):
        await h.validate(f"http://{host}/")
    with pytest.raises(BlockedError):  # ANY private answer blocks (rebinding-style multi-record)
        await h.validate("http://mixed.example/")


@pytest.mark.parametrize("url", ["http://2130706433/", "http://0x7f.1/", "http://[::ffff:127.0.0.1]/", "http://127.1/"])
async def test_encoded_loopback_blocked(url):
    async def resolver(host, port):  # emulate OS resolver semantics for encoded forms
        return {"2130706433": ["127.0.0.1"], "0x7f.1": ["127.0.0.1"], "127.1": ["127.0.0.1"],
                "::ffff:127.0.0.1": ["::ffff:127.0.0.1"]}[host]

    with pytest.raises(BlockedError):
        await SafeHttp(FetchCfg(), resolver=resolver).validate(url)


@respx.mock
async def test_redirect_to_private_is_blocked():
    h = make({"public.example": ["93.184.216.34"], "evil.example": ["10.0.0.9"]})
    respx.get("https://93.184.216.34:443/start").mock(
        return_value=httpx.Response(302, headers={"location": "https://evil.example/secret"}))
    with pytest.raises(BlockedError):
        await h.get("https://public.example/start")
    await h.aclose()


@respx.mock
async def test_redirect_followed_and_size_capped():
    h = make({"a.example": ["93.184.216.34"], "b.example": ["93.184.216.35"]})
    respx.get("https://93.184.216.34:443/x").mock(return_value=httpx.Response(301, headers={"location": "https://b.example/y"}))
    respx.get("https://93.184.216.35:443/y").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/html"}, content=b"a" * 50_000))
    h.cfg.max_bytes = 20_000  # body is 2.5x the cap: truncated, not rejected (>4x is rejected up front)
    r = await h.get("https://a.example/x")
    assert r.url == "https://b.example/y" and r.truncated and len(r.body) == 20_000
    await h.aclose()


@respx.mock
async def test_bad_content_type_and_status():
    h = make({})
    respx.get("https://93.184.216.34:443/bin").mock(return_value=httpx.Response(200, headers={"content-type": "application/zip"}, content=b"x"))
    respx.get("https://93.184.216.34:443/404").mock(return_value=httpx.Response(404))
    with pytest.raises(FetchError, match="content type"):
        await h.get("https://e.example/bin")
    with pytest.raises(FetchError, match="404"):
        await h.get("https://e.example/404")
    await h.aclose()


@respx.mock
async def test_too_many_redirects():
    h = make({})
    respx.get("https://93.184.216.34:443/loop").mock(return_value=httpx.Response(302, headers={"location": "/loop"}))
    with pytest.raises(FetchError, match="redirects"):
        await h.get("https://e.example/loop")
    await h.aclose()


@respx.mock
async def test_robots_txt():
    h = make({})
    respx.get("https://93.184.216.34:443/robots.txt").mock(
        return_value=httpx.Response(200, headers={"content-type": "text/plain"}, content=b"User-agent: *\nDisallow: /private\n"))
    assert await h.robots_allows("https://e.example/public")
    assert not await h.robots_allows("https://e.example/private/x")
    await h.aclose()

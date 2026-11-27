"""读网页真的去取的那一步（harness/fetch/client.py）：对着本地的假网站（tests/fetch_rig.py），不连任何真的网站。

要守住的：
  - 发出去的只有一个干净的 GET：User-Agent 是 ZhiFox/版本号，不带 Cookie、认证、Accept-Language；
  - 每一跳都从头再查：一个"公网"的网站把你跳到局域网、元数据地址、file: 上，一律不去，而且根本不连；
  - 真的规矩拦得住本机：127.0.0.1、解析到 127.0.0.1 的名字、一半公网一半本机的 DNS 回答，都是一个字节都不发；
  - 查过的就是连上的（DNS 重绑定）：一次读只解析一次，连的就是那个地址；
  - 上限：大小（说了 Content-Length 的直接不读，没说的读到上限截住）、解压炸弹、跳转次数、时间（一个一点一点发的网站拖不住）。
"""

from __future__ import annotations

import gzip
import socket
import time

import pytest
from fetch_rig import LOCAL, FakeSite, fetcher, page, redirect

from harness.fetch import client, guard
from harness.fetch.client import Fetcher, FetchError


@pytest.fixture
def site():
    made = FakeSite().start()
    yield made
    made.stop()


# ---- 发出去的是什么 ----------------------------------------------------------------


def test_a_plain_get_with_nothing_about_you(site):
    site.routes["/"] = page("<title>你好</title><p>正文</p>")
    got = fetcher("site.test").get(site.url("/?q=1"))
    assert got.status == 200 and b"<p>" in got.body and got.content_type == "text/html"
    sent = site.requests[0]
    assert sent["path"] == "/?q=1"
    headers = sent["headers"]
    assert headers["user-agent"].startswith("ZhiFox/")
    assert headers["host"] == f"site.test:{site.port}"  # Host 头写域名，不写连上的那个地址
    for name in ("cookie", "authorization", "accept-language", "proxy-authorization"):
        assert name not in headers, name


def test_fragment_is_not_sent(site):
    site.routes["/doc"] = page("<p>x</p>")
    fetcher("site.test").get(guard.parse(site.url("/doc#part-2")).url)
    assert site.requests[0]["path"] == "/doc"


# ---- 跳转：每一跳都从头再查 --------------------------------------------------------------


def test_follows_redirects_and_resolves_every_hop(site):
    site.routes["/a"] = redirect("/b")
    site.routes["/b"] = redirect(site.url("/c", host="other.test"), status=301)
    site.routes["/c"] = page("<p>到了</p>")
    made = fetcher("site.test", "other.test")
    got = made.get(site.url("/a"))
    assert got.url == site.url("/c", host="other.test")
    assert got.hops == (site.url("/a"), site.url("/b"))
    assert [request["path"] for request in site.requests] == ["/a", "/b", "/c"]
    assert made.resolved == ["site.test", "site.test", "other.test"]  # 每一跳都重新解析、重新检查


@pytest.mark.parametrize(
    ("location", "words"),
    [
        ("http://private.test/admin", "局域网"),
        ("http://169.254.169.254/latest/meta-data/", "元数据"),
        ("http://[::1]/", "这台电脑自己"),
        ("http://localhost.test:1/", "这台电脑自己"),
        ("file:///etc/passwd", "http:// 和 https://"),
    ],
    ids=["private-name", "metadata", "ipv6-loopback", "name-to-loopback", "file"],
)
def test_redirect_to_a_place_it_must_not_go(site, location, words):
    site.routes["/go"] = redirect(location)
    made = fetcher("site.test", extra={"private.test": ["10.0.0.7"], "localhost.test": ["127.0.0.2"]})
    with pytest.raises(guard.Refused, match=words) as caught:
        made.get(site.url("/go"))
    assert "跳转到了" in str(caught.value)  # 说清楚是网站把你跳过去的
    assert [request["path"] for request in site.requests] == ["/go"]  # 跳到的地方一个字节都没发


def test_too_many_redirects(site):
    site.routes["/loop"] = redirect("/loop")
    with pytest.raises(FetchError, match="跳转了 3 次"):
        fetcher("site.test", redirects=3).get(site.url("/loop"))
    assert len(site.requests) == 4  # 第一次加上跟着跳的三次


# ---- 真的规矩：本机、局域网一律不去，而且根本不连 ----------------------------------------------


def test_the_real_rules_refuse_loopback_literals(site):
    site.routes["/"] = page("<p>不该被读到</p>")
    for url in (f"http://127.0.0.1:{site.port}/", f"http://[::ffff:127.0.0.1]:{site.port}/", f"http://0.0.0.0:{site.port}/"):
        with pytest.raises(guard.Refused, match="没有读"):
            Fetcher().get(url)
    assert site.requests == []


def test_the_real_rules_refuse_a_name_that_resolves_to_loopback(site):
    site.routes["/"] = page("<p>不该被读到</p>")
    real_rules = Fetcher(resolve=lambda host, port: [LOCAL])  # 假的 DNS，真的 policy
    with pytest.raises(guard.Refused, match="解析到 127.0.0.1"):
        real_rules.get(site.url("/"))
    assert site.requests == []


def test_half_public_half_private_answer_is_refused(site):
    # 一个名字同时解析到公网和本机：多半有问题（DNS 重绑定常这么干），整个不读
    made = Fetcher(resolve=lambda host, port: ["93.184.216.34", LOCAL], policy=guard.refusal)
    with pytest.raises(guard.Refused):
        made.get(site.url("/"))
    assert site.requests == []


def test_connects_to_the_address_it_checked(site, monkeypatch):
    # DNS 重绑定：解析一次、查一遍，连的就是那个地址（不让 socket 自己再解析一遍域名）
    site.routes["/"] = page("<p>x</p>")
    connected: list[tuple] = []
    real = socket.create_connection

    def recording(address, *args, **kwargs):
        connected.append(address)
        return real(address, *args, **kwargs)

    monkeypatch.setattr(client.socket, "create_connection", recording)
    made = fetcher("site.test")
    made.get(site.url("/"))
    assert connected == [(LOCAL, site.port)]
    assert made.resolved == ["site.test"]


def test_tests_never_resolve_real_names():
    # conftest 把系统的解析换掉了：哪个测试忘了换 resolve，也碰不到真的 DNS
    with pytest.raises(FetchError, match="不许解析真的域名"):
        Fetcher().get("https://example.com/")


@pytest.mark.parametrize("host", ["8.8.8.8%x.attacker.test", "[2001:4860::8888%25x.attacker.test]"], ids=["v4", "v6"])
def test_a_percent_after_an_ip_is_not_a_second_lookup(site, monkeypatch, host):
    # "8.8.8.8%x.attacker.test" 前半截是公网的 8.8.8.8，以前按它查过就放行了；可连的时候 socket.create_connection
    # 把整串交给系统的 getaddrinfo，系统把它当成一个域名（attacker.test 的子域名）再去解析一遍，
    # 那边的 DNS 想答什么就答什么（127.0.0.1）：查的和连的不是同一个。这里用一个假的 getaddrinfo 扮演那个 DNS
    site.routes["/"] = page("<p>本机上的服务，不该被读到</p>")
    looked_up: list[object] = []
    real = socket.getaddrinfo

    def attacker_dns(name, port, *args, **kwargs):
        looked_up.append(name)
        if isinstance(name, str) and "attacker.test" in name:
            return real(LOCAL, port, *args, **kwargs)
        return real(name, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", attacker_dns)
    with pytest.raises(guard.Refused):
        Fetcher().get(f"http://{host}:{site.port}/")  # 真的规矩
    assert site.requests == []
    assert not any("attacker.test" in str(name) for name in looked_up)


# ---- 上限 ------------------------------------------------------------------------------


def test_declared_too_big_is_not_read(site):
    site.routes["/big"] = page(b"x" * 5000)
    with pytest.raises(FetchError, match="太大"):
        fetcher("site.test", limit=1000).get(site.url("/big"))


def test_undeclared_too_big_is_cut(site):
    def stream(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/plain")
        handler.send_header("Connection", "close")
        handler.end_headers()  # 不说多大，一直发到关上连接
        for _ in range(10):
            handler.wfile.write(b"y" * 1000)

    site.routes["/stream"] = stream
    got = fetcher("site.test", limit=2500).get(site.url("/stream"))
    assert got.cut and got.body == b"y" * 2500


def test_gzip_is_decoded(site):
    site.routes["/z"] = page(gzip.compress("压缩过的网页".encode()), Content_Encoding="gzip")
    got = fetcher("site.test").get(site.url("/z"))
    assert got.body.decode() == "压缩过的网页" and not got.cut


def test_gzip_bomb_stops_at_the_limit(site):
    # 几 KB 解压出几 MB（真的炸弹是几 GB，道理一样）：最多解出上限那么多，不把内存撑满
    site.routes["/bomb"] = page(gzip.compress(b"\0" * 5_000_000), Content_Encoding="gzip")
    started = time.monotonic()
    got = fetcher("site.test", limit=10_000).get(site.url("/bomb"))
    assert got.cut and len(got.body) == 10_000
    assert time.monotonic() - started < 10


def test_unknown_compression(site):
    site.routes["/br"] = page(b"\x8b\x00", Content_Encoding="br")
    with pytest.raises(FetchError, match="br"):
        fetcher("site.test").get(site.url("/br"))


def test_a_dripping_server_cannot_hold_it(site):
    # 每隔一会儿发一个字节：每一下都不超时，可是整个读网页有总的时限，看门的到点就掐断
    def drip(handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "text/plain")
        handler.end_headers()
        try:
            for _ in range(200):
                handler.wfile.write(b".")
                handler.wfile.flush()
                time.sleep(0.1)
        except OSError:
            pass  # 读的那一边掐断了

    site.routes["/drip"] = drip
    started = time.monotonic()
    with pytest.raises(FetchError, match="太久"):
        fetcher("site.test", timeout=1.0).get(site.url("/drip"))
    assert time.monotonic() - started < 6  # 1 秒的时限；慢的 CI 机器上也远不到 20 秒


def test_slow_headers_cannot_hold_it(site):
    def slow_headers(handler):
        try:
            handler.wfile.write(b"HTTP/1.1 200 OK\r\n")
            for index in range(200):
                handler.wfile.write(f"X-Slow-{index}: 1\r\n".encode())
                handler.wfile.flush()
                time.sleep(0.1)
        except OSError:
            pass

    site.routes["/slow"] = slow_headers
    started = time.monotonic()
    with pytest.raises(FetchError, match="太久"):
        fetcher("site.test", timeout=1.0).get(site.url("/slow"))
    assert time.monotonic() - started < 6


def test_nothing_listening():
    with socket.socket() as probe:
        probe.bind((LOCAL, 0))
        port = probe.getsockname()[1]  # 拿到一个端口再关掉：上面没有人在听
    with pytest.raises(FetchError, match="连不上"):
        fetcher("site.test").get(f"http://site.test:{port}/")


def test_name_not_found():
    with pytest.raises(FetchError, match="找不到 nowhere.test"):
        fetcher("site.test").get("http://nowhere.test/")


def test_https_always_does_tls(site):
    # https 的网址一定先握手、按域名验证证书：假网站说的是明文 HTTP，握手不成，什么都没读到
    site.routes["/"] = page("<p>明文</p>")
    with pytest.raises(FetchError):
        fetcher("site.test").get(f"https://site.test:{site.port}/")



def test_never_hands_a_name_to_the_socket(monkeypatch):
    # 查过的必须就是连上的：交给 socket 的只能是查过的 IP 地址本身。交给它一个名字，它会再去解析一遍，
    # 那一次解析出来的地址谁也没查过（1.1.1.1%.localhost 曾经被当成 1.1.1.1 放行，系统把整串解析到 ::1）
    connected: list[tuple] = []

    def recording(address, *args, **kwargs):
        connected.append(address)
        raise OSError("测试里不连")

    monkeypatch.setattr(client.socket, "create_connection", recording)
    with pytest.raises(guard.Refused):
        Fetcher().get("http://1.1.1.1%.localhost/")
    # 一个"解析"出了名字而不是地址的 resolve（假的 DNS 写错了、将来换了实现）：照样不交给 socket
    with pytest.raises((guard.Refused, FetchError)):
        Fetcher(resolve=lambda host, port: ["localhost"], policy=lambda address: None).get("http://site.test/")
    assert connected == []

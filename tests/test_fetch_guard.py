"""读网页能去哪儿（harness/fetch/guard.py）：只拿地址和网址本身测，不解析域名、不连任何网站。

这是读网页的安全关卡（规矩写在 harness/fetch/__init__.py 开头）：模型可能被网页里的一句话带偏，让它去读
http://169.254.169.254/（云服务器的元数据，里面有临时密钥）、http://127.0.0.1:8000/（你电脑上的服务），
或者用一种不常见的写法（2130706433、0x7f.1）把 127.0.0.1 藏起来。这里把每一种都列出来，一种一种地看它拦不拦得住，
也看公网上的普通地址没有被误拦（拦错了，读网页就没法用）。

client.py 会对域名解析出来的每一个地址都问一遍 refusal，所以 refusal 是最后那一道；parse 拦下奇怪的写法是多加的一道。
"""

from __future__ import annotations

import ipaddress

import pytest

from harness.browser import urls
from harness.fetch import guard

# ---- refusal：一个 IP 地址能不能去 ---------------------------------------------

# 一律不去的。键是测试的名字（固定的 ids），值是地址
BLOCKED = {
    "v4-loopback": "127.0.0.1",
    "v4-loopback-top": "127.255.255.254",
    "v6-loopback": "::1",
    "v4-unspecified": "0.0.0.0",  # 很多系统上连 0.0.0.0 就是连本机
    "v6-unspecified": "::",
    "v4-private-10": "10.1.2.3",
    "v4-private-172-low": "172.16.0.1",
    "v4-private-172-high": "172.31.255.255",
    "v4-private-192": "192.168.1.1",
    "metadata-aws-gcp-azure": "169.254.169.254",
    "v4-shared-cgnat": "100.64.0.1",
    "metadata-alibaba": "100.100.100.200",
    "metadata-oracle": "192.0.0.192",
    "v4-multicast": "224.0.0.1",
    "v4-multicast-ssdp": "239.255.255.250",
    "v4-broadcast": "255.255.255.255",
    "v4-reserved": "240.0.0.1",
    "v4-benchmark": "198.18.0.1",
    "v4-documentation": "192.0.2.1",
    "v6-link-local": "fe80::1",
    "v6-link-local-zone": "fe80::1%en0",  # 带着网卡名的写法
    "v6-unique-local": "fd00::1",
    "metadata-aws-v6": "fd00:ec2::254",
    "v6-unique-local-fc": "fc00::1",
    "v6-multicast": "ff02::1",
    "v4-mapped-loopback": "::ffff:127.0.0.1",
    "v4-mapped-private": "::ffff:10.0.0.1",
    "v4-mapped-metadata": "::ffff:169.254.169.254",
    "nat64-loopback": "64:ff9b::7f00:1",
    "nat64-metadata": "64:ff9b::a9fe:a9fe",
    "6to4-loopback": "2002:7f00:1::1",
    "v6-documentation": "2001:db8::1",
    "v6-teredo": "2001::1",  # Teredo 里藏着的 IPv4 是打乱过的，没法按它判断，整段不去
    "v4-compatible-loopback": "::127.0.0.1",
    "v6-site-local": "fec0::1",
    "garbage": "not-an-ip",  # 看不懂的一律不去
    "empty": "",
}

# 公网上的普通地址：能去（返回 None）。有几个就贴在不能去的那几段的边上，拦多了就会失败
ALLOWED = {
    "v4-example": "93.184.216.34",
    "v4-google-dns": "8.8.8.8",
    "v4-cloudflare-dns": "1.1.1.1",
    "v4-below-172-16": "172.15.255.255",
    "v4-above-172-31": "172.32.0.1",
    "v4-below-cgnat": "100.63.255.255",
    "v4-above-cgnat": "100.128.0.1",
    "v6-cloudflare-dns": "2606:4700:4700::1111",
    "v6-google-dns": "2001:4860:4860::8888",
    "v4-mapped-public": "::ffff:8.8.8.8",  # 藏在 IPv6 里的 IPv4 按里面那个判断：公网的照样能去
    "nat64-public": "64:ff9b::808:808",
    "6to4-public": "2002:808:808::1",
}


@pytest.mark.parametrize("address", list(BLOCKED.values()), ids=list(BLOCKED))
def test_blocked_addresses_are_refused(address: str):
    why = guard.refusal(address)
    assert isinstance(why, str) and why  # 不能去的一定说出原因（写给模型看的）


@pytest.mark.parametrize("address", list(ALLOWED.values()), ids=list(ALLOWED))
def test_public_addresses_are_allowed(address: str):
    assert guard.refusal(address) is None


@pytest.mark.parametrize(
    ("address", "reason"),
    [
        ("127.0.0.1", guard.LOOPBACK),
        ("::ffff:127.0.0.1", guard.LOOPBACK),  # 藏在 IPv6 里的，说法和里面那个 IPv4 一样
        ("169.254.169.254", guard.LINK_LOCAL),
        ("100.100.100.200", guard.SHARED),
        ("192.168.1.1", guard.PRIVATE),
        ("fd00:ec2::254", guard.PRIVATE),
        ("fe80::1", guard.LINK_LOCAL),
        ("ff02::1", guard.MULTICAST),
        ("0.0.0.0", guard.UNSPECIFIED),
    ],
    ids=["loopback", "mapped-loopback", "metadata", "alibaba", "lan", "aws-v6", "v6-link-local", "v6-multicast", "unspecified"],
)
def test_refusal_says_why(address: str, reason: str):
    assert guard.refusal(address) == reason


def test_reason_text_is_plain_chinese_for_the_model():
    # 模型要能看懂为什么不读：本机说"这台电脑自己"，元数据的地址说明那里是"元数据"接口
    assert "这台电脑自己" in (guard.refusal("127.0.0.1") or "")
    assert "元数据" in (guard.refusal("169.254.169.254") or "")
    assert "元数据" in (guard.refusal("100.100.100.200") or "")


def test_v6_loopback_is_called_loopback():
    assert guard.refusal("::1") == guard.LOOPBACK


@pytest.mark.parametrize("address", ["224.0.0.1", "100.64.0.1", "64:ff9b::7f00:1", "2001::1"], ids=["multicast", "cgnat", "nat64", "teredo"])
def test_python_saying_global_does_not_matter(address: str):
    # 这几个地址，Python 自带的 is_global / is_private 在 3.11 和 3.13 上的说法不一样（比如 3.11 认为 224.0.0.1 是 global）。
    # guard 自己列了一张表，不问 Python：这里直接断言它们都不去，不管 Python 怎么说
    assert guard.refusal(address) is not None


@pytest.mark.parametrize("claim", [True, False], ids=["python-says-global", "python-says-private"])
def test_decisions_do_not_use_is_private_or_is_global(monkeypatch, claim: bool):
    # 更狠一点：把 Python 的 is_global、is_private 换成一律说同一句话，结果一个都不能变。
    # 能变就说明 guard 在偷偷依赖它们，换一个 Python 版本，安全关卡就可能变了
    for cls in (ipaddress.IPv4Address, ipaddress.IPv6Address):
        monkeypatch.setattr(cls, "is_global", property(lambda self: claim))
        monkeypatch.setattr(cls, "is_private", property(lambda self: not claim))
    for address in BLOCKED.values():
        assert guard.refusal(address) is not None, address
    for address in ALLOWED.values():
        assert guard.refusal(address) is None, address


# ---- parse：网址怎么理解 -------------------------------------------------------


def test_parse_https_defaults():
    target = guard.parse("https://example.com")
    assert target == guard.Target(url="https://example.com/", scheme="https", host="example.com", port=443, path="/", literal=False)
    assert target.host_header == "example.com"


def test_parse_port_query_and_fragment():
    target = guard.parse("http://Example.COM:8080/a/b?x=1&y=2#section")
    assert target.scheme == "http"
    assert target.host == "example.com"  # 主机名一律小写
    assert target.port == 8080
    assert target.path == "/a/b?x=1&y=2"  # 请求行里的那一截带着 ? 后面的
    assert target.url == "http://example.com:8080/a/b?x=1&y=2"  # # 后面的不发给网站
    assert target.host_header == "example.com:8080"  # 不是默认端口才写出来


@pytest.mark.parametrize(
    ("url", "port", "header", "full"),
    [
        ("http://example.com/", 80, "example.com", "http://example.com/"),
        ("http://example.com:80/", 80, "example.com", "http://example.com/"),
        ("https://example.com:443/", 443, "example.com", "https://example.com/"),
        ("http://example.com:443/", 443, "example.com:443", "http://example.com:443/"),  # 443 只对 https 是默认的
        ("https://example.com:8443/", 8443, "example.com:8443", "https://example.com:8443/"),
    ],
    ids=["http-default", "http-explicit-80", "https-explicit-443", "http-on-443", "https-custom"],
)
def test_parse_default_ports(url: str, port: int, header: str, full: str):
    target = guard.parse(url)
    assert target.port == port
    assert target.host_header == header  # 默认端口不写进 Host 头
    assert target.url == full


def test_parse_scheme_is_case_insensitive():
    assert guard.parse("HTTPS://example.com/").scheme == "https"


def test_parse_ipv6_literal():
    target = guard.parse("https://[2606:4700:4700::1111]/x")
    assert target.host == "2606:4700:4700::1111"  # 解析、连接用的不带方括号
    assert target.literal is True
    assert target.host_header == "[2606:4700:4700::1111]"  # Host 头里要带方括号
    assert target.url == "https://[2606:4700:4700::1111]/x"
    assert guard.parse("https://[2606:4700:4700::1111]:8443/").host_header == "[2606:4700:4700::1111]:8443"


def test_parse_ipv4_literal():
    target = guard.parse("http://93.184.216.34/page")
    assert target.literal is True
    assert target.host == "93.184.216.34"
    # parse 只看写法，地址能不能去由 client.py 用 refusal 查：所以本机的地址在这里照样解析得出来
    assert guard.parse("http://127.0.0.1:8000/").literal is True


def test_parse_domain_is_not_literal():
    assert guard.parse("https://example.com/").literal is False


def test_parse_trailing_dot_dropped():
    assert guard.parse("https://example.com./").host == "example.com"


def test_parse_idn_host_becomes_punycode():
    # 汉字域名：解析、证书、Host 头都要用 xn-- 的写法
    target = guard.parse("https://例子.测试/")
    assert target.host == "xn--fsqu00a.xn--0zwm56d"
    assert target.host_header == "xn--fsqu00a.xn--0zwm56d"
    assert guard.parse("https://bücher.de/").host == "xn--bcher-kva.de"


def test_parse_percent_encodes_path_and_query():
    # 空格、汉字按 UTF-8 写成 %XX；已经写成 %XX 的不再写一遍（不然 %E4 会变成 %25E4，网站收到的就不是原来的网址了）
    target = guard.parse("https://example.com/a b/路径/%E4%B8%AD?q=中 文&x=%20")
    assert target.path == "/a%20b/%E8%B7%AF%E5%BE%84/%E4%B8%AD?q=%E4%B8%AD%20%E6%96%87&x=%20"
    assert "%25" not in target.url
    assert target.url == "https://example.com" + target.path


def test_parse_keeps_reserved_path_characters():
    target = guard.parse("https://example.com/a/b;c=d,e@f:g/~h?k=v&l=(m)")
    assert target.path == "/a/b;c=d,e@f:g/~h?k=v&l=(m)"


def test_parse_query_without_path():
    target = guard.parse("https://example.com?x=1")
    assert target.path == "/?x=1"
    assert target.url == "https://example.com/?x=1"


REFUSED = {
    "ftp": "ftp://example.com/file",
    "file": "file:///etc/passwd",
    "javascript": "javascript:alert(1)",
    "data": "data:text/html,<p>hi</p>",
    "no-scheme-left": "example.com/docs",  # parse 只收整理过的网址：没写协议的先过 normalize
    "user-and-password": "http://user:pass@example.com/",
    "user-only": "http://user@example.com/",
    "bare-at": "http://@example.com/",
    "no-host": "http:///x",
    "port-too-big": "http://example.com:99999/",
    "port-not-a-number": "http://example.com:abc/",
    "broken-ipv6": "http://[::1/",
    "decimal-ip": "http://2130706433/",
    "hex-ip": "http://0x7f.1/",
    "hex-whole-ip": "http://0x7f000001/",
    "short-ip": "http://127.1/",
    "octal-ip": "http://0177.0.0.1/",
    "numeric-tld": "http://example.123/",
    "decimal-ip-trailing-dot": "http://2130706433./",
}


@pytest.mark.parametrize("url", list(REFUSED.values()), ids=list(REFUSED))
def test_parse_refuses(url: str):
    with pytest.raises(guard.Refused) as caught:
        guard.parse(url)
    assert str(caught.value)  # 拒绝的时候一定说为什么（写给模型看的）


def test_parse_refuses_too_long():
    url = "https://example.com/" + "a" * urls.MAX_URL_CHARS
    assert len(url) > urls.MAX_URL_CHARS
    with pytest.raises(guard.Refused, match="太长"):
        guard.parse(url)


def test_parse_accepts_url_at_the_length_limit():
    head = "https://example.com/"
    url = head + "a" * (urls.MAX_URL_CHARS - len(head))
    assert len(url) == urls.MAX_URL_CHARS
    assert guard.parse(url).host == "example.com"


def test_parse_refusal_messages_explain():
    with pytest.raises(guard.Refused, match="http:// 和 https://"):
        guard.parse("file:///etc/passwd")
    with pytest.raises(guard.Refused, match="用户名或者密码"):
        guard.parse("http://user:pass@example.com/")
    with pytest.raises(guard.Refused, match="标准写法"):
        guard.parse("http://2130706433/")


@pytest.mark.parametrize("url", ["http://１２７.１/", "http://２１３０７０６４３３/", "http://127。0。0。1/"], ids=["fullwidth-short", "fullwidth-decimal", "ideographic-dots"])
def test_parse_refuses_numeric_hosts_after_idna(url: str):
    try:
        target = guard.parse(url)
    except guard.Refused:
        return
    # 没拒绝的话，至少要认出这是一个 IP 地址（literal），而不是当成域名交给 DNS
    assert target.literal, target


# ---- normalize、site_of ----------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        ("example.com/docs", "https://example.com/docs"),
        ("  https://example.com/x  ", "https://example.com/x"),
        ("http://example.com/", "http://example.com/"),
        ("example.com:8080/x", "https://example.com:8080/x"),
    ],
    ids=["no-scheme", "spaces", "http-kept", "host-and-port"],
)
def test_normalize(given: str, expected: str):
    assert guard.normalize(given) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://www.example.com/a",
        "http://example.com/b",
        "https://example.com:8443/c",
        "https://docs.example.com/",
        "ftp://example.com/",
    ],
    ids=["www-https", "plain-http", "custom-port", "subdomain", "not-web"],
)
def test_site_of_matches_browser(url: str):
    # 读网页问不问你按"网站"算，和浏览器"打开一个新的网站"必须是同一个算法，不然允许过的网站在两边不一样
    assert guard.site_of(url) == urls.site_of(url)


def test_site_of_rules():
    assert guard.site_of("https://www.example.com/a") == guard.site_of("https://example.com/b") == "https://example.com"
    assert guard.site_of("http://example.com/") != guard.site_of("https://example.com/")  # http 和 https 不算同一个
    assert guard.site_of("https://docs.example.com/") != guard.site_of("https://example.com/")  # 子域名不算同一个


@pytest.mark.parametrize("url", ["http://exa mple.com/", "http://exa<mple.com/", "http://exa%41mple.com/"], ids=["space", "angle", "percent"])
def test_parse_refuses_odd_characters_in_the_host(url: str):
    # 换成 ASCII 以后的域名只能有字母、数字、点、连字符（和下划线）：别的字符解析不出来，也可能是想骗过检查
    with pytest.raises(guard.Refused):
        guard.parse(url)


def test_v4_compatible_does_not_swallow_unspecified():
    # :: 和 ::1 在"IPv4 兼容"那一段的开头，但它们是"未指定"和这台电脑自己，原因要说对
    assert guard.refusal("::") == guard.UNSPECIFIED
    assert guard.refusal("::1") == guard.LOOPBACK

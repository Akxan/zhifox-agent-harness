"""自定义模型接口（harness/endpoints/）的单元测试：地址的规矩、表单的检查、存在哪儿、怎么猜能力、导出时密钥不出去。

连真的服务器（假的 OpenAI 格式服务器）的测试在 test_endpoints_chat.py（对话、工具、退一步）和 test_endpoints_web.py（网页的接口）。
"""

from __future__ import annotations

import io
import json
import stat
import zipfile

import pytest
from fake_openai import FakeOpenAI
from test_export import export_bytes

from harness.endpoints import EndpointError, EndpointStore, urls
from harness.endpoints.detect import apply_lmstudio, apply_ollama, from_listing, guess
from harness.endpoints.probe import LMSTUDIO, OLLAMA, probe
from harness.endpoints.spec import (
    Endpoint,
    EndpointModel,
    draft,
    from_form,
    merged,
    slug,
)
from harness.export import NOTE, PLACEHOLDER

# 编造的、长得像真密钥的字符串。拼起来写，免得源文件里出现一整串"像密钥"的东西
GATEWAY_KEY = "sk-" + "gateway-" + "FAKE" * 6


def form(**changes) -> dict:
    base = {"name": "Ollama", "base_url": "http://localhost:11434/v1", "api_key": "", "models": []}
    return {**base, **changes}


# ---- 地址的规矩 ---------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:11434/v1",
        "http://127.0.0.1:1234/v1",
        "http://[::1]:8080/v1",
        "http://0.0.0.0:11434/v1",
        "http://192.168.1.20:8000/v1",
        "http://10.0.0.5/v1",
        "http://172.20.3.4:8000/v1",
        "http://100.101.102.103:11434/v1",  # Tailscale
        "http://[fd7a:115c:a1e0::1]:11434/v1",  # Tailscale 的 IPv6（内网地址）
        "http://mac-studio.local:11434/v1",
        "http://llm.internal/v1",
        "http://gpu-box:8000/v1",  # 没有点的单个名字：只有内网的 DNS 认得
        "http://ollama.localhost/v1",
        "https://llm.example.com/v1",
        "https://api.example.com",
    ],
)
def test_allowed_addresses(url):
    checked = urls.check(url)
    assert checked.url == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "http://llm.example.com/v1",  # 外面的地址只能 https
        "http://8.8.8.8/v1",
        "http://169.254.169.254/v1",  # 链路本地（云服务器的元数据服务）不算内网
        "http://[::ffff:8.8.8.8]/v1",  # 包着一个外面的 IPv4
        "http://[2001:4860::1]/v1",
        "ftp://localhost/v1",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "localhost:11434",  # 没写协议
        "https://user:secret@llm.example.com/v1",  # 地址里带着密码
        "https://llm.example.com/v1?key=abc",
        "https://llm.example.com/v1#frag",
        "https://llm.example.com:99999/v1",
        "https://llm.example.com/v 1",
        "",
        None,
    ],
)
def test_refused_addresses(url):
    with pytest.raises(urls.UrlError):
        urls.check(url)


def test_where_an_address_is():
    assert urls.where_is("localhost") == urls.where_is("127.0.0.1") == urls.where_is("::1") == urls.HERE
    assert urls.where_is("192.168.0.2") == urls.where_is("studio.local") == urls.where_is("gpu-box") == urls.LAN
    assert urls.where_is("api.example.com") == urls.where_is("1.1.1.1") == urls.REMOTE
    assert urls.check("http://LOCALHOST:11434/v1/").origin == "http://localhost:11434"
    assert urls.check("http://[::1]:8080/v1").origin == "http://[::1]:8080"
    assert urls.root_of("http://localhost:11434/v1") == "http://localhost:11434"


def test_the_refusal_explains_why_http_is_not_allowed():
    with pytest.raises(urls.UrlError) as caught:
        urls.check("http://llm.example.com/v1")
    assert "https://" in caught.value.message and "llm.example.com" in caught.value.message
    assert caught.value.message.key  # 是 t() 译好的话：页面换语言时能重译


# ---- 表单 ------------------------------------------------------------------


def test_a_form_becomes_an_endpoint():
    endpoint = from_form(form(models=[{"id": "qwen3:8b", "vision": False, "tools": True, "pdf": False, "context": 40960, "price": None}]), None, set())
    assert (endpoint.id, endpoint.provider_id, endpoint.name, endpoint.base_url) == ("ollama", "custom-ollama", "Ollama", "http://localhost:11434/v1")
    assert endpoint.local and endpoint.where == urls.HERE and endpoint.api_key == ""
    assert endpoint.models == [EndpointModel(id="qwen3:8b", vision=False, tools=True, pdf=False, context=40960)]
    # 本机的接口不花钱：价格没填时按 0 算（换模型的面板写"本地，不计费"）
    assert endpoint.price_of("qwen3:8b") == {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_write": 0.0}


def test_remote_endpoints_have_no_price_until_you_give_one():
    endpoint = from_form(form(name="公司网关", base_url="https://llm.example.com/v1", api_key=GATEWAY_KEY), None, set())
    assert endpoint.id == "endpoint"  # 名字全是中文，取不出编号
    assert not endpoint.local and endpoint.price_of("gpt-x") is None
    endpoint.models.append(EndpointModel(id="gpt-x", price={"input": 2.0, "output": 8.0}))
    assert endpoint.price_of("gpt-x") == {"input": 2.0, "output": 8.0, "cache_read": 2.0, "cache_write": 2.0}
    assert endpoint.hint() == "…" + GATEWAY_KEY[-4:]


@pytest.mark.parametrize(
    "changes",
    [
        {"name": ""},
        {"name": "x" * 41},
        {"base_url": "http://llm.example.com/v1"},
        {"api_key": "有中文的密钥"},
        {"api_key": "has space"},
        {"models": [{"id": "a b", "vision": False, "tools": True, "pdf": False}]},
        {"models": [{"id": "", "vision": False, "tools": True, "pdf": False}]},
        {"models": [{"id": "m", "vision": "yes", "tools": True, "pdf": False}]},
        {"models": [{"id": "m", "vision": False, "tools": True, "pdf": False, "context": 10}]},
        {"models": [{"id": "m", "vision": False, "tools": True, "pdf": False, "context": True}]},
        {"models": [{"id": "m", "vision": False, "tools": True, "pdf": False, "price": {"input": -1, "output": 0}}]},
        {"models": [{"id": "m", "vision": False, "tools": True, "pdf": False, "price": {"input": 1}}]},
        {"models": [{"id": "m", "vision": False, "tools": True, "pdf": False, "price": {"input": float("nan"), "output": 1}}]},
        {"models": [{"id": "m", "vision": False, "tools": True, "pdf": False}] * 2},
        {"models": "qwen3"},
    ],
)
def test_bad_forms_are_refused_with_a_reason(changes):
    with pytest.raises(EndpointError) as caught:
        from_form(form(**changes), None, set())
    assert str(caught.value.message)


def test_the_id_comes_from_the_name_and_never_collides():
    assert slug("My LM Studio!", set()) == "my-lm-studio"
    assert slug("Ollama", {"ollama", "ollama-2"}) == "ollama-3"
    assert slug("公司网关", set()) == "endpoint"


def test_keeping_the_key_only_works_on_the_same_server():
    saved = from_form(form(name="网关", base_url="https://llm.example.com/v1", api_key=GATEWAY_KEY), None, set())
    kept = from_form(form(name="网关", base_url="https://llm.example.com/v2", api_key={"keep": True}), saved, set())
    assert kept.api_key == GATEWAY_KEY and kept.id == saved.id  # 编号不变
    # 换了服务器还想带着旧的密钥：不行，要重新填（旧的密钥不会发给新的地址）
    with pytest.raises(EndpointError):
        from_form(form(name="网关", base_url="https://other.example.com/v1", api_key={"keep": True}), saved, set())
    with pytest.raises(EndpointError):
        draft({"base_url": "https://other.example.com/v1", "api_key": {"keep": True}}, saved)
    # 换了服务器、重新填了，或者清空了，都行
    assert from_form(form(name="网关", base_url="https://other.example.com/v1", api_key=""), saved, set()).api_key == ""


def test_syncing_keeps_your_settings_and_drops_what_the_server_no_longer_has():
    mine = [EndpointModel(id="qwen3:8b", vision=True, tools=False, context=None, price={"input": 1.0, "output": 1.0}), EndpointModel(id="gone")]
    found = [EndpointModel(id="qwen3:8b", vision=False, tools=True, context=40960), EndpointModel(id="new", vision=True)]
    assert merged(mine, found) == [
        EndpointModel(id="qwen3:8b", vision=True, tools=False, context=40960, price={"input": 1.0, "output": 1.0}),
        EndpointModel(id="new", vision=True),
    ]


# ---- 存在哪儿 ----------------------------------------------------------------


def test_the_store_writes_a_private_file_and_reads_it_back(tmp_path):
    path = tmp_path / "data" / "endpoints.json"
    store = EndpointStore(path)
    saved = store.save(form(name="网关", base_url="https://llm.example.com/v1", api_key=GATEWAY_KEY, models=[{"id": "gpt-x", "vision": True, "tools": True, "pdf": True, "price": {"input": 2, "output": 8}}]))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    again = EndpointStore(path)
    assert again.list() == [saved] and again.problem is None
    assert again.prices() == {"custom-endpoint/gpt-x": {"input": 2.0, "output": 8.0, "cache_read": 2.0, "cache_write": 2.0}}
    store.save(form(name="Ollama"))
    with pytest.raises(EndpointError):
        store.save(form(name="ollama"))  # 名字不分大小写也不能重
    store.delete("custom-ollama")
    assert [item.name for item in EndpointStore(path).list()] == ["网关"]
    with pytest.raises(EndpointError):
        store.delete("custom-ollama")


def test_learning_and_prices_are_written_down(tmp_path):
    store = EndpointStore(tmp_path / "endpoints.json")
    store.save(form(models=[{"id": "qwen3:8b", "vision": False, "tools": True, "pdf": False}]))
    store.learned("custom-ollama", "qwen3:8b", tools=False)
    store.learned("custom-ollama", "llama2", tools=False)  # 列表里没有的也记下（Agent 文件里写的）
    store.learned("custom-ollama", "qwen3:8b", stream_usage=False)
    store.learned("custom-gone", "x", tools=False)  # 接口不在了：不出错
    store.set_price("custom-ollama", "qwen3:8b", {"input": 0.5, "output": 1.0})
    endpoint = EndpointStore(tmp_path / "endpoints.json").get("custom-ollama")
    assert [(model.id, model.tools) for model in endpoint.models] == [("qwen3:8b", False), ("llama2", False)]
    assert endpoint.stream_usage is False and endpoint.price_of("qwen3:8b")["input"] == 0.5


def test_an_unreadable_file_is_never_overwritten(tmp_path):
    path = tmp_path / "endpoints.json"
    path.write_text("{ not json", encoding="utf-8")
    store = EndpointStore(path)
    assert store.list() == [] and store.problem and str(path) in store.problem
    with pytest.raises(EndpointError):
        store.save(form())
    assert path.read_text(encoding="utf-8") == "{ not json"  # 你手写的东西原样留着


def test_entries_that_break_the_rules_are_skipped_but_kept(tmp_path):
    path = tmp_path / "endpoints.json"
    good = {"id": "ollama", "name": "Ollama", "base_url": "http://localhost:11434/v1", "models": []}
    bad = {"id": "leak", "name": "Leak", "base_url": "http://llm.example.com/v1", "api_key": "sk-x", "models": []}
    path.write_text(json.dumps({"version": 1, "endpoints": [good, bad]}), encoding="utf-8")
    store = EndpointStore(path)
    assert [item.id for item in store.list()] == ["ollama"]  # 外面的 http:// 不用
    assert "Leak" in store.problem
    store.save(form(name="LM Studio", base_url="http://localhost:1234/v1"))
    written = json.loads(path.read_text(encoding="utf-8"))["endpoints"]
    assert bad in written  # 读不懂的那一项原样写回去，不删


# ---- 猜能力 -----------------------------------------------------------------


def test_guessing_from_names_and_listings():
    assert guess("llava:13b").vision and guess("qwen2.5vl:7b").vision and not guess("qwen3:8b").vision
    assert not guess("llama2:7b").tools and guess("qwen3:8b").tools
    assert from_listing({"id": "nomic-embed-text"}) is None  # 向量模型不是用来聊天的
    vllm = from_listing({"id": "meta-llama/Llama-3.1-8B-Instruct", "max_model_len": 131072})
    assert vllm.id == "meta-llama/Llama-3.1-8B-Instruct" and vllm.context == 131072
    router = from_listing({"id": "x/y", "context_length": 64000, "architecture": {"input_modalities": ["text", "image"]}, "supported_parameters": ["temperature"]})
    assert router.vision and not router.tools and router.context == 64000


def test_ollama_and_lm_studio_say_what_their_models_can_do():
    model = EndpointModel(id="gemma3:4b")
    apply_ollama(model, {"capabilities": ["completion", "vision"], "model_info": {"gemma3.context_length": 131072}})
    assert (model.vision, model.tools, model.context) == (True, False, 131072)
    models = [EndpointModel(id="qwen2-vl"), EndpointModel(id="text-embedding-nomic"), EndpointModel(id="other")]
    kept = apply_lmstudio(models, {"data": [
        {"id": "qwen2-vl", "type": "vlm", "max_context_length": 32768, "capabilities": ["tool_use"]},
        {"id": "text-embedding-nomic", "type": "embeddings"},
    ]})
    assert [(item.id, item.vision, item.tools, item.context) for item in kept] == [("qwen2-vl", True, True, 32768), ("other", False, True, None)]


def test_probing_an_ollama_server_costs_nothing():
    server = FakeOpenAI([], models=["qwen3:8b", "gemma3:4b", "nomic-embed-text"], ollama={
        "qwen3:8b": {"capabilities": ["completion", "tools"], "model_info": {"qwen3.context_length": 40960}},
        "gemma3:4b": {"capabilities": ["completion", "vision"], "model_info": {"gemma3.context_length": 131072}},
    }).start()
    try:
        found = probe(Endpoint(id="o", name="Ollama", base_url=server.base_url + "/v1"))
        assert found.listed and found.server == OLLAMA
        assert [(m.id, m.vision, m.tools, m.context) for m in found.models] == [("qwen3:8b", False, True, 40960), ("gemma3:4b", True, False, 131072)]
        assert server.requests == []  # 只问了列表和介绍，一个聊天请求都没发
        assert set(server.paths) <= {"/v1/models", "/api/version", "/api/show"}
    finally:
        server.stop()


def test_odd_names_from_the_server_are_left_out():
    server = FakeOpenAI([], models=["good:1b", "has space", "x" * 300, "good:1b"]).start()
    try:
        found = probe(Endpoint(id="o", name="怪服务器", base_url=server.base_url + "/v1"))
        assert [model.id for model in found.models] == ["good:1b"]  # 存不进去的名字不列，重复的只列一次
    finally:
        server.stop()


def test_probing_lm_studio_and_a_server_without_a_model_list():
    studio = FakeOpenAI([], models=["qwen2-vl-7b", "text-embedding-x"], lmstudio=[{"id": "qwen2-vl-7b", "type": "vlm", "max_context_length": 32768}]).start()
    bare = FakeOpenAI([], listing=False).start()
    try:
        found = probe(Endpoint(id="s", name="LM Studio", base_url=studio.base_url + "/v1"))
        assert found.server == LMSTUDIO and [(m.id, m.vision, m.context) for m in found.models] == [("qwen2-vl-7b", True, 32768)]
        empty = probe(Endpoint(id="b", name="网关", base_url=bare.base_url + "/v1"))
        assert not empty.listed and empty.models == []  # 没有 /models：模型名要你自己填，但连接是通的
    finally:
        studio.stop()
        bare.stop()


def test_probing_explains_what_went_wrong():
    rejecting = FakeOpenAI([]).start()
    try:
        with pytest.raises(EndpointError) as caught:
            probe(Endpoint(id="r", name="网关", base_url=rejecting.base_url + "/v1", api_key="bad-key-" + "X" * 12))
        assert "401" in caught.value.message
        assert "X" * 12 not in caught.value.message  # 网关把收到的密钥写在了出错说明里：给人看之前遮掉
    finally:
        rejecting.stop()
    with pytest.raises(EndpointError) as caught:
        probe(Endpoint(id="n", name="Ollama", base_url="http://127.0.0.1:9/v1"))  # 没有人在听的端口
    assert "ollama serve" in caught.value.message


def test_redirects_are_not_followed(tmp_path):
    elsewhere = FakeOpenAI([]).start()
    redirecting = FakeOpenAI([], redirect=elsewhere.base_url + "/v1/models").start()
    try:
        with pytest.raises(EndpointError) as caught:
            probe(Endpoint(id="r", name="网关", base_url=redirecting.base_url + "/v1", api_key="secret-gateway-key"))
        assert "307" in caught.value.message and elsewhere.base_url in caught.value.message
        assert elsewhere.keys == []  # 带着密钥的请求没有跟着跳过去
    finally:
        elsewhere.stop()
        redirecting.stop()


# ---- 导出 ------------------------------------------------------------------


def test_the_endpoint_key_is_not_in_the_export(tmp_path):
    data = tmp_path / "data"
    store = EndpointStore(data / "endpoints.json")
    store.save(form(name="网关", base_url="https://llm.example.com/v1", api_key=GATEWAY_KEY, models=[{"id": "gpt-x", "vision": True, "tools": True, "pdf": False}]))
    store.save(form(name="Ollama", api_key="lm-studio-short"))

    raw, work = export_bytes(data)
    archive = zipfile.ZipFile(io.BytesIO(raw))
    assert "endpoints.json" in work.masked
    entries = json.loads(archive.read("endpoints.json"))["endpoints"]
    assert [entry["api_key"] for entry in entries] == [PLACEHOLDER, PLACEHOLDER]
    assert entries[0]["base_url"] == "https://llm.example.com/v1" and entries[0]["models"][0]["id"] == "gpt-x"  # 别的照样留着
    everything = b"".join(archive.read(name) for name in archive.namelist())
    assert GATEWAY_KEY.encode() not in everything and b"lm-studio-short" not in everything
    assert "endpoints.json" in archive.read(NOTE).decode()
    assert EndpointStore(data / "endpoints.json").list()[0].api_key == GATEWAY_KEY  # 原来的文件不动

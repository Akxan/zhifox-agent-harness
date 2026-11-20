"""设置的保存、API key 的写入和费用估算的测试。"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta, timezone

from harness.pricing import estimate, is_offpeak, price_of, usage_key
from harness.providers import PROVIDERS, has_key
from harness.providers.base import Usage
from harness.settings import Settings, key_hint, save_api_key, valid_api_key


def test_settings_round_trip(tmp_path):
    path = tmp_path / "data" / "settings.json"
    settings = Settings(path, "anthropic", "claude-opus-5-5")
    assert (settings.provider, settings.model, settings.models, settings.prices) == ("anthropic", "claude-opus-5-5", {}, {})

    settings.provider, settings.model = "deepseek", "deepseek-flash"
    settings.models["deepseek"] = ["deepseek-flash"]
    settings.prices["deepseek-flash"] = {"input": 0.3, "output": 1.2}
    settings.save()

    again = Settings(path, "anthropic", "claude-opus-5-5")  # 保存过的选择优先于默认值
    assert (again.provider, again.model) == ("deepseek", "deepseek-flash")
    assert again.models == {"deepseek": ["deepseek-flash"]}
    assert again.prices == {"deepseek-flash": {"input": 0.3, "output": 1.2}}
    assert "KEY" not in path.read_text(encoding="utf-8").upper()  # 设置文件里没有任何密钥


def test_a_broken_settings_file_is_ignored(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{ 不是合法的 JSON", encoding="utf-8")
    settings = Settings(path, "anthropic", "claude-sonnet-5-5")
    assert settings.model == "claude-sonnet-5-5"
    settings.save()
    assert json.loads(path.read_text(encoding="utf-8"))["model"] == "claude-sonnet-5-5"


def test_saving_keys_only_touches_its_own_line(tmp_path):
    env_file = tmp_path / ".env"
    save_api_key(env_file, "OPENAI_API_KEY", "sk-first-0000")  # 文件不存在时会创建
    assert env_file.read_text(encoding="utf-8") == "OPENAI_API_KEY=sk-first-0000\n"
    assert os.environ["OPENAI_API_KEY"] == "sk-first-0000"  # 在当前程序里立即生效

    env_file.write_text(
        "# 注释\nANTHROPIC_API_KEY=sk-ant-keep\nexport OPENAI_API_KEY = sk-old\nHARNESS_WEB=0\n", encoding="utf-8"
    )
    save_api_key(env_file, "OPENAI_API_KEY", "sk-second-1111")
    assert env_file.read_text(encoding="utf-8") == (
        "# 注释\nANTHROPIC_API_KEY=sk-ant-keep\nHARNESS_WEB=0\nOPENAI_API_KEY=sk-second-1111\n"
    )

    save_api_key(env_file, "OPENAI_API_KEY", "")  # 空值表示删除
    assert env_file.read_text(encoding="utf-8") == "# 注释\nANTHROPIC_API_KEY=sk-ant-keep\nHARNESS_WEB=0\n"
    assert "OPENAI_API_KEY" not in os.environ


def test_key_checks_and_hints(monkeypatch):
    assert valid_api_key("sk-ant-usr-1Wz_abc-DEF123")
    for bad in ("", "short", "有中文的key12345", "with space 12345", "line\nbreak-12345", "x" * 500):
        assert not valid_api_key(bad)

    assert key_hint("DEEPSEEK_API_KEY") == ""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-12345678")
    assert key_hint("DEEPSEEK_API_KEY") == "…5678"
    assert has_key(PROVIDERS["deepseek"]) and not has_key(PROVIDERS["gemini"])
    assert not has_key(PROVIDERS["anthropic"])
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-12345678")  # Claude 还接受登录令牌
    assert has_key(PROVIDERS["anthropic"])


def test_cost_estimate():
    usage = {
        "claude-opus-5-5": Usage(input_tokens=1_000_000, cache_read_tokens=2_000_000, cache_write_tokens=100_000,
                                 output_tokens=50_000, searches=65),
        "deepseek-flash": Usage(input_tokens=1_000_000, cache_read_tokens=1_000_000, output_tokens=1_000_000),
        "deepseek-flash@offpeak": Usage(input_tokens=1_000_000, cache_read_tokens=1_000_000, output_tokens=1_000_000),
        "some-new-model": Usage(input_tokens=5, output_tokens=5),
    }
    # Claude 用内置价格：4 + 2×0.2 + 0.1×5 + 0.05×20，再加 65 次搜索每次 1 美分
    claude = 4 + 0.4 + 0.5 + 1.0 + 0.65
    # DeepSeek 也有内置价格，分高峰（0.30 / 0.006 / 1.20）和闲时（一半）
    flash, flash_offpeak = 0.30 + 0.006 + 1.20, 0.15 + 0.003 + 0.60
    cost, unpriced = estimate(usage, custom={})
    assert round(cost, 6) == round(claude + flash + flash_offpeak, 6)
    assert unpriced == ["some-new-model"]  # 没有价格的不计入，并且会告诉用户

    # 用户填了价格之后就计入；没单独填缓存价格的，按普通输入的价格算
    custom = {"some-new-model": {"input": 1_000_000, "output": 2_000_000}}
    cost, unpriced = estimate(usage, custom)
    assert round(cost, 6) == round(claude + flash + flash_offpeak + 15, 6) and unpriced == []
    # 自己填的价格全天都按它算：闲时的那份用量也按它
    custom = {"deepseek-flash": {"input": 1, "output": 1, "cache_read": 1}}
    assert round(estimate({"deepseek-flash@offpeak": Usage(input_tokens=1_000_000)}, custom)[0], 6) == 1

    # 用户填的价格优先于内置的
    assert price_of("claude-opus-5-5", {})["output"] == 20.0
    assert price_of("claude-opus-5-5", {"claude-opus-5-5": {"input": 1, "output": 2}})["output"] == 2.0
    assert price_of("unknown", {}) is None
    # 旧名字按新名字的价格算
    assert price_of("deepseek-v4-flash", {}) == price_of("deepseek-flash", {})
    assert price_of("deepseek-v4-pro", {}, offpeak=True) == {"input": 0.66, "output": 1.98, "cache_read": 0.022, "cache_write": 0.66}
    # 不分时段的模型，闲时价就是原价
    assert price_of("claude-opus-5-5", {}, offpeak=True) == price_of("claude-opus-5-5", {})


def test_deepseek_off_peak_hours():
    utc = UTC
    # 高峰：周一到周五 UTC 01:00–04:00、06:00–10:00（北京时间 9:00–12:00、14:00–18:00）
    friday = datetime(2026, 11, 20, tzinfo=utc)
    peak = [friday.replace(hour=1), friday.replace(hour=3, minute=59), friday.replace(hour=6), friday.replace(hour=9, minute=59)]
    off = [friday.replace(hour=0, minute=59), friday.replace(hour=4), friday.replace(hour=5, minute=30), friday.replace(hour=10), friday.replace(hour=23)]
    off += [datetime(2026, 11, 21, 2, tzinfo=utc), datetime(2026, 11, 22, 7, tzinfo=utc)]  # 周六、周日全天闲时
    assert [is_offpeak(moment) for moment in peak] == [False] * len(peak)
    assert all(is_offpeak(moment) for moment in off)
    # 别的时区写的时间，换算成 UTC 再看：北京时间周五 9:30 是高峰，12:30 是闲时
    beijing = timezone(timedelta(hours=8))
    assert not is_offpeak(datetime(2026, 11, 20, 9, 30, tzinfo=beijing))
    assert is_offpeak(datetime(2026, 11, 20, 12, 30, tzinfo=beijing))

    # 记用量时：分时段的模型在闲时记到"@offpeak"下面，别的模型不管什么时候都照常记
    assert usage_key("deepseek-flash", friday.replace(hour=2)) == "deepseek-flash"
    assert usage_key("deepseek-flash", friday.replace(hour=12)) == "deepseek-flash@offpeak"
    assert usage_key("deepseek-v4-flash", friday.replace(hour=12)) == "deepseek-v4-flash@offpeak"
    assert usage_key("claude-opus-5-5", friday.replace(hour=12)) == "claude-opus-5-5"
    assert usage_key("some-new-model", friday.replace(hour=12)) == "some-new-model"

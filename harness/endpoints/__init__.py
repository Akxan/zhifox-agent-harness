"""自定义模型接口：Ollama、LM Studio、vLLM、llama.cpp 的服务器，公司自己的网关——任何 OpenAI 格式的地址。

为什么要有：内置的四家都在别人的服务器上，对话要发到外面去。接上一个本机或者内网里的模型，数据就留在你的
电脑或者公司的网络里；接上公司的网关，就能用公司买好的额度。

和内置的几家是怎么接上的：每个接口在程序里就是一个 Provider（见 providers/__init__.py 的 find / every），
kind 是 "openai"，走的是 GPT、Gemini、DeepSeek 共用的那个适配层（providers/openai_compat.py）。不同的地方：

  - 能力按模型记，不按家记（同一个 Ollama 上有的模型看得懂图、有的连工具都用不了，见 spec.py）；
  - 密钥是可选的，存在 data/endpoints.json，不进 .env、也不进环境变量（理由见 store.py）；
  - 有的服务器不认某些参数：不认 stream_options 的，不再要用量；模型用不了工具的，这段对话不发工具。
    第一次被拒绝时自动退一步，并记进 endpoints.json，下次不再碰壁（见 openai_compat.py 的 _degrade）；
  - 价格：本机和内网的默认不花钱，外面的网关默认"没有价格"（见 spec.Endpoint.price_of）。

这几个文件：
  urls.py     地址的规矩：外面的一律 https://，明文的 http:// 只许本机和内网
  spec.py     一个接口长什么样、页面交来的表单怎么检查
  store.py    存在哪儿、怎么改（data/endpoints.json，600）
  client.py   连接口的客户端：别的几家的密钥不带过去、不跟重定向、本机和内网不走代理
  detect.py   猜一个模型能做什么
  probe.py    测试连接、同步模型列表（只发不花钱的请求）
  errors.py   出错时给人看的话

别的程序（比如评测框架）要指定"用哪个接口的哪个模型"，写法和 Agent 文件里的 model 一样：
"custom-<编号>/<模型名>"（custom-ollama/qwen3:8b），用 providers.find 找到 Provider、make_backend 建适配层就行。
"""

from __future__ import annotations

from .spec import PREFIX, Endpoint, EndpointError, EndpointModel
from .store import EndpointStore
from .urls import UrlError

__all__ = ["PREFIX", "Endpoint", "EndpointError", "EndpointModel", "EndpointStore", "UrlError"]

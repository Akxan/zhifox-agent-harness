"""遮住工具结果里像密钥的东西：在模型看到之前就换掉。

为什么要有。命令的输出、读到的文件、MCP 服务器和网页交回来的东西里，常常夹着一把密钥：cat 了一个配置文件、
env 打印了环境变量、git remote -v 的地址里带着令牌、日志里有一行 Authorization: Bearer ……。它一旦进了对话历史，
就会被发给模型厂商、写进会话文件、显示在页面和聊天里、跟着导出——而且历史只在末尾追加，进去了就改不掉
（见 agent.py 开头的规矩）。所以要在结果产生的那一刻换掉：agent 循环在 tool_ended 这个记录点之前调用这里，
模型、历史、会话文件、页面、聊天通道、运行记录，以后的钩子，看到的都是遮住以后的那一份。遮的是"刚产生的结果"，
不是改前面的历史，所以只追加的规矩照样守着。

认三类：

  1. 各家密钥的固定格式（KEY_SHAPES）：Anthropic、OpenAI、DeepSeek、Gemini、GitHub、Slack、Telegram、Discord、
     AWS、JWT、PEM 私钥……导出数据（export.py）和 MCP 的设置页（mcp/config.py）认密钥用的也是这一张表。
  2. 已经知道的密钥的值：tools/shell.SECRET_ENV_VARS 里的环境变量、.env 里和环境变量里名字像密钥的、自定义模型接口的密钥、
     日志里登记过要藏起来的（聊天机器人的 token……，见 log.hidden）。按值比较：值从不写进日志，也不出现在遮住以后的文字里，
     换上去的记号只写"是哪个变量的值"。
  3. 紧跟在 password=、token:、--api-key、Bearer 这类字后面、看起来是随机的一串（够长、字母和数字都有、信息量够高），
     还有网址里的密码（https://用户:密码@host）。

宁可漏掉一个可疑的，也不要把正常的输出遮坏：git 的提交号、UUID 只是长得随机，旁边没有 token= 这类字的一律不碰；
连成一大段的 base64（图片、文件的内容，REDACT_DATA_RUN）里碰巧长得像密钥的也不碰；"request.headers.get"这种像代码的、
"${API_KEY}"这种占位符、"********"也不碰。几个"多像才算"的数在 config.py。

遮成什么：‹已遮住：疑似 OpenAI 密钥›。有遮的，结果末尾再附一句给模型看的说明（中文，和别的工具结果一样不翻译）。

这不是防线，只是一层兜底：模型要是存心绕开（让命令把密钥 base64 一下再打印），这里认不出来。真正挡住密钥的还是
.env 和 data/ 碰不到、命令看不到各家的密钥（见 permissions.py、tools/shell.py）。

设置里可以关掉（Settings.redact_secrets，默认开着）。关掉以后这里原样交回。
"""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import overload

from .config import (
    REDACT_DATA_RUN,
    REDACT_MIN_ENTROPY,
    REDACT_MIN_KNOWN,
    REDACT_MIN_VALUE_CHARS,
    env_file_values,
)

MARK = "‹已遮住：{label}›"
MARKED = re.compile(r"‹已遮住：[^‹›\n]{1,60}›")  # 换上去的记号（改文件的工具认它，见 tools/files.py）

# ---- 1. 各家密钥的固定格式 ------------------------------------------------------------
# （名字，样子）。前后不能紧挨着别的字母数字（不然一长串里的一截也会被认出来）。顺序有讲究：具体的排在前面，
# "sk-" 开头的几家里 Anthropic、OpenAI、DeepSeek 先认，剩下的才算"API 密钥"
_EDGE_BEFORE = r"(?<![A-Za-z0-9_\-])"
_EDGE_AFTER = r"(?![A-Za-z0-9_\-])"
KEY_SHAPES: list[tuple[str, re.Pattern[str]]] = [
    (label, re.compile((pattern if pattern.startswith("(?") else _EDGE_BEFORE + pattern) + _EDGE_AFTER))
    for label, pattern in [
        ("疑似 Anthropic 密钥", r"sk-ant-[a-z]{2,10}\d{2}-[A-Za-z0-9_\-]{20,}"),
        ("疑似 OpenAI 密钥", r"sk-(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{20,}"),
        ("疑似 OpenAI 密钥", r"sk-[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20}"),
        ("疑似 DeepSeek 密钥", r"sk-[0-9a-f]{32}"),
        ("疑似 OpenAI 密钥", r"sk-[A-Za-z0-9]{48}"),
        # 别的 "sk-" 开头的（很多家都用这个前缀）。要有数字：sk-learn-preprocessing 这种名字不算
        ("疑似 API 密钥", r"sk-(?=[A-Za-z0-9_\-]*\d)[A-Za-z0-9_\-]{20,}"),
        ("疑似 Google（Gemini）密钥", r"AIza[0-9A-Za-z_\-]{35}"),
        ("疑似 GitHub 令牌", r"gh[pousr]_[A-Za-z0-9]{36,}"),
        ("疑似 GitHub 令牌", r"github_pat_[A-Za-z0-9_]{22,}"),
        ("疑似 Slack 令牌", r"xox[abposr]-[A-Za-z0-9\-]{10,}"),
        ("疑似 Slack 令牌", r"xapp-\d-[A-Za-z0-9\-]{10,}"),
        # 机器人的编号、冒号、35 个字符（以 AA 开头）：https://core.telegram.org/bots/api#authorizing-your-bot
        # 它常写在网址里，紧跟着 bot（https://api.telegram.org/bot<token>/getMe），所以前面可以是 bot
        ("疑似 Telegram 机器人令牌", r"(?:(?<=bot)|(?<![A-Za-z0-9_\-]))\d{6,12}:AA[A-Za-z0-9_\-]{33}"),
        ("疑似 Discord 机器人令牌", r"[MN][A-Za-z\d_\-]{23,27}\.[A-Za-z\d_\-]{6}\.[A-Za-z\d_\-]{27,40}"),
        ("疑似 AWS 访问密钥", r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
        ("疑似 Stripe 密钥", r"(?:sk|rk)_live_[A-Za-z0-9]{20,}"),
        # 三段 base64url，前两段解出来是 {"…（eyJ）
        ("疑似 JWT 令牌", r"eyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    ]
]
# 只在配置文件里用的、更宽的几条（导出、MCP 的设置页）：那里多遮几个不碍事，工具的输出里就太容易遮错了
LOOSE_SHAPES: list[re.Pattern[str]] = [
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
]


def mask_shapes(text: str, replacement: str) -> str:
    """把长得像密钥的（固定格式的，加上更宽的那几条）都换成 replacement。给配置文件用（export.py、mcp/config.py）。"""
    for _, shape in KEY_SHAPES:
        text = shape.sub(replacement, text)
    for shape in LOOSE_SHAPES:
        text = shape.sub(replacement, text)
    return text


# PEM 格式的私钥：从 BEGIN 那一行到 END 那一行整个换掉。没有 END（输出被截断了）的，换到 base64 的那几行结束
_PEM_BEGIN = re.compile(r"-----BEGIN ((?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?)-----")
_PEM_BODY = re.compile(r"(?:\r?\n(?:[A-Za-z0-9+/=]{1,128}|[A-Za-z\-]+: [^\n]*|))*")

# ---- 2. 按值认 ------------------------------------------------------------------------
# 环境变量和 .env 里名字像这样的，值当作密钥（SECRET_ENV_VARS 里的不看名字，一律算）
SECRET_NAME = re.compile(r"token|secret|passw(?:or)?d|pwd|api_?key|access_?key|private_?key|credential", re.IGNORECASE)

# ---- 3. 紧跟在提示词后面的 --------------------------------------------------------------
_PASSWORD_WORDS = r"password|passwd|passphrase|pwd"
_KEY_WORDS = rf"{_PASSWORD_WORDS}|secret|token|api[_\-]?key|apikey|access[_\-]?key|private[_\-]?key|client[_\-]?secret|auth"
_VALUE = r"[^\s\"'`,;&<>(){}\[\]‹›|]+"
# 名字以这些字结尾、后面是 = 或 : 的（DB_PASSWORD=…、"api_key": "…"、token: …、--password=…）。
# 名字只从一个词的开头认起（前面不是字母、数字、_ . -），而且要以字母开头：这样一长串 "------" 里不用每个位置都试一遍
_ASSIGNED = re.compile(rf"(?i)(?<![A-Za-z0-9_.\-])(-{{0,2}}(?=[A-Za-z_])[A-Za-z0-9_.\-]{{0,64}}?(?:{_KEY_WORDS}))([\"']?[ \t]*[:=][ \t]*[\"']?)({_VALUE})")
# 命令行参数：--token xxx、-password xxx（--token=xxx 上面那条已经认了）
_FLAG = re.compile(rf"(?i)(?<![\w\-])(--?[\w\-]{{0,64}}?(?:{_KEY_WORDS}))([ \t]+)({_VALUE})")
# Authorization: Bearer xxx、Basic xxx
_BEARER = re.compile(r"(?i)(\b(?:authorization[\"']?[ \t]*[:=][ \t]*[\"']?(?:bearer|basic|token)?|bearer)[ \t]+)([A-Za-z0-9._~+/\-]+=*)")
# 网址里的密码：scheme://用户:密码@主机
_URL_PASSWORD = re.compile(r"(\b[A-Za-z][A-Za-z0-9+.\-]*://[^/\s:@'\"‹›]+:)([^/\s@'\"‹›]+)(@)")
# 这些名字虽然以 token 结尾，但值不是密钥：翻页用的、防跨站的
_NOT_SECRET_NAME = re.compile(r"(?i)page|next|cursor|continuation|csrf|xsrf|sync|resume|max|count|num")
_UUID = re.compile(r"(?i)[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_CODE_PATH = re.compile(r"[A-Za-z_$][A-Za-z_$]*(?:\.[A-Za-z_$][\w$]*)+")  # process.env.API_KEY_V2、config.secrets.token
_CONSTANT = re.compile(r"[A-Z][A-Z0-9_]*")  # GITHUB_TOKEN2：是变量的名字，不是值
_FILLER = re.compile(r"[x*•.#\-_]+|.*(?:example|placeholder|your|changeme|redacted).*", re.IGNORECASE)
_PLACEHOLDER_START = ("$", "%", "{", "<", "*", "/", "~", "./", "../", "\\")

# 一段数据（图片、文件的内容）：data: 地址，或者连着很长的 base64
# （只从一段的开头试：前面已经是 base64 字符的位置不用再试，不然一兆的文字要试上亿次）
_DATA = re.compile(rf"data:[\w.+/\-]+;base64,[A-Za-z0-9+/]+=*|(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{{{REDACT_DATA_RUN},}}=*")
# 密码比令牌短：单独的门槛（"hunter2" 仍然太短，"Summer2024!" 够了）
_PASSWORD_MIN_CHARS = 8
_PASSWORD_MIN_ENTROPY = 2.5


def entropy(text: str) -> float:
    """每个字符平均的信息量（香农熵，比特）。随机的密钥高（base64 将近 6），英文单词、代码低。"""
    if not text:
        return 0.0
    counts = Counter(text)
    size = len(text)
    return -sum(n / size * math.log2(n / size) for n in counts.values())


def secretish(value: str, password: bool = False) -> bool:
    """跟在 password=、token: 后面的这个值，像不像一个真的密钥。"""
    value = value.strip("\"'")
    if password:
        long_enough = len(value) >= _PASSWORD_MIN_CHARS and entropy(value) >= _PASSWORD_MIN_ENTROPY
        mixed = any(c.isalpha() for c in value) and any(not c.isalpha() for c in value)
    else:
        long_enough = len(value) >= REDACT_MIN_VALUE_CHARS and entropy(value) >= REDACT_MIN_ENTROPY
        mixed = any(c.isalpha() for c in value) and any(c.isdigit() for c in value)
    if not (long_enough and mixed):
        return False
    if value.startswith(_PLACEHOLDER_START) or "://" in value:
        return False  # 占位符（${KEY}、%s、<token>）、路径、网址
    # UUID、像代码的（process.env.KEY）、变量的名字（GITHUB_TOKEN2）、填空用的（xxxx、your-key-here）
    return not (_UUID.fullmatch(value) or _CODE_PATH.fullmatch(value) or _CONSTANT.fullmatch(value) or _FILLER.fullmatch(value))


# ---- 遮 ------------------------------------------------------------------------------


@dataclass
class Redaction:
    """遮完的结果：文字（没有遮的话就是原文），一共遮了几处，各是什么。"""

    text: str
    count: int = 0
    kinds: Counter = field(default_factory=Counter)


Known = Callable[[], Iterable[tuple[str, str]]]  # 交出（记号上写的名字，值）


class Redactor:
    """遮密钥的那一个函数，连着"有哪些已知的密钥"和"开没开"。App 建一个，主助手、定时任务、聊天、子 Agent 共用。

    env_file：.env 在哪儿（名字像密钥的值按值认）。known：别的已知的密钥（自定义接口的密钥……），每次遮之前现取，
    你刚在页面上加的接口马上算数。enabled：开没开（每次现看设置）。
    """

    def __init__(self, env_file: Path | None = None, known: Known | None = None, enabled: Callable[[], bool] = lambda: True):
        self.env_file = env_file
        self.known = known
        self.enabled = enabled

    def __call__(self, text: str) -> Redaction:
        if not text or not self.enabled():
            return Redaction(text)
        return redact(text, self.values())

    def values(self) -> list[tuple[str, str]]:
        """已经知道的密钥：（记号上写的名字，值）。长的在前：一个是另一个的一部分时，先换长的。"""
        from . import log
        from .tools.shell import (
            SECRET_ENV_VARS,  # 用到时才导入：tools 会反过来导入很多东西
        )

        found: dict[str, str] = {}
        for name, value in os.environ.items():
            if name in SECRET_ENV_VARS or SECRET_NAME.search(name):
                found.setdefault(value, f"{name} 的值")
        if self.env_file is not None:
            try:
                entries = env_file_values(self.env_file)
            except (OSError, UnicodeDecodeError):
                entries = []
            for name, value in entries:
                if name in SECRET_ENV_VARS or SECRET_NAME.search(name):
                    found.setdefault(value, f"{name} 的值")
        if self.known is not None:
            for label, value in self.known():
                found.setdefault(value, label)
        for value in log.hidden():
            found.setdefault(value, "密钥")
        usable = [(label, value) for value, label in found.items() if _usable_known(value)]
        return sorted(usable, key=lambda pair: len(pair[1]), reverse=True)


def _usable_known(value: str) -> bool:
    """一个已知的值能不能拿来按值认：够长、不是路径、不是开关这种词。"""
    value = value.strip()
    return len(value) >= REDACT_MIN_KNOWN and not value.startswith(("/", "~", "./")) and not value.isalpha()


def redact(text: str, known: Iterable[tuple[str, str]] = ()) -> Redaction:
    """遮住 text 里像密钥的东西。known 是已经知道的密钥（见 Redactor.values）。"""
    kinds: Counter = Counter()
    text = _pem(text, kinds)
    known = list(known)
    out, last = [], 0
    for data in _DATA.finditer(text):  # 一段段的数据原样留着，只看它们之间的文字
        out.append(_plain(text[last : data.start()], known, kinds))
        out.append(data.group(0))
        last = data.end()
    out.append(_plain(text[last:], known, kinds))
    count = sum(kinds.values())
    return Redaction("".join(out) if count else text, count, kinds)


# 交进来的是什么样子，交回去的还是什么样子（字典还是字典）。写两遍是给类型检查看的：调用的人给的是一个字典时，它就知道拿回来的也是
@overload
def redact_data(value: dict, redactor: Callable[[str], Redaction]) -> tuple[dict, int]: ...
@overload
def redact_data(value: object, redactor: Callable[[str], Redaction]) -> tuple[object, int]: ...
def redact_data(value: object, redactor: Callable[[str], Redaction]) -> tuple[object, int]:
    """一份数据（浏览器的标签页情况：标题、网址……）里的每一段文字都遮一遍。返回（遮好的数据，一共遮了几处）。
    网址里常带着令牌（?access_token=…），这份数据也会交给模型，所以和结果的文字一样要遮。"""
    if isinstance(value, str):
        done = redactor(value)
        return done.text, done.count
    if isinstance(value, dict):
        total, out = 0, {}
        for key, item in value.items():
            out[key], count = redact_data(item, redactor)
            total += count
        return out, total
    if isinstance(value, list):
        total, items = 0, []
        for item in value:
            masked, count = redact_data(item, redactor)
            items.append(masked)
            total += count
        return items, total
    return value, 0


def note(redaction: Redaction) -> str:
    """附在结果末尾、写给模型看的一句话（中文，不翻译）。没遮就是空的。"""
    if not redaction.count:
        return ""
    kinds = "、".join(f"{label} {n} 处" if n > 1 else label for label, n in redaction.kinds.most_common())
    return (
        f"\n\n[智狐遮住了这段结果里 {redaction.count} 处像密钥的内容（{kinds}），换成了 ‹已遮住：…› 记号，原文没有发给你。"
        "不要设法读出原文；改文件时也不要把这些记号写回去（那会把真正的密钥换掉）。要用到这个密钥的操作，请让用户自己做。]"
    )


def mark(label: str) -> str:
    return MARK.format(label=label)


def _pem(text: str, kinds: Counter) -> str:
    out, last = [], 0
    for begin in _PEM_BEGIN.finditer(text):
        if begin.start() < last:
            continue  # 已经在上一段里换掉了
        end = text.find(f"-----END {begin.group(1)}-----", begin.end())
        if end >= 0:
            stop = end + len(f"-----END {begin.group(1)}-----")
        else:
            body = _PEM_BODY.match(text, begin.end())
            assert body is not None  # 给类型检查看的：_PEM_BODY 整个是 (...)*，一个字都不匹配也算对得上，不会是 None
            stop = body.end()
        out.append(text[last : begin.start()])
        out.append(mark("私钥"))
        kinds["私钥"] += 1
        last = stop
    out.append(text[last:])
    return "".join(out)


def _plain(text: str, known: list[tuple[str, str]], kinds: Counter) -> str:
    """一段不是数据的文字：按值认、认格式、认提示词后面的。每一步换上的记号，后面几步都认不出来（字符里有 ‹ ›）。"""
    if not text:
        return text
    for label, value in known:
        if value in text:
            kinds[label] += text.count(value)
            text = text.replace(value, mark(label))
    for label, shape in KEY_SHAPES:
        # label=label 把这一轮的 label 定下来。写成函数而不是带默认值的 lambda：那种 lambda 的类型 mypy 推不出来
        def counted(match: re.Match[str], label: str = label) -> str:
            return _count(kinds, label)

        text = shape.sub(counted, text)
    text = _ASSIGNED.sub(lambda match: _guess(match, kinds), text)
    text = _FLAG.sub(lambda match: _guess(match, kinds), text)
    text = _BEARER.sub(lambda match: match.group(1) + _count(kinds, "疑似令牌") if secretish(match.group(2)) else match.group(0), text)
    text = _URL_PASSWORD.sub(lambda match: _url(match, kinds), text)
    return text


def _count(kinds: Counter, label: str) -> str:
    kinds[label] += 1
    return mark(label)


def _guess(match: re.Match[str], kinds: Counter) -> str:
    """name=value 这样的一处：名字说明它是密码、令牌还是密钥，值也像，才换掉值（名字和等号留着，看得出是哪一项）。"""
    name, between, value = match.group(1), match.group(2), match.group(3)
    word = name.lower()
    if _NOT_SECRET_NAME.search(word):
        return match.group(0)
    password = bool(re.search(rf"(?:{_PASSWORD_WORDS})$", word))
    if not secretish(value, password=password):
        return match.group(0)
    label = "疑似密码" if password else "疑似令牌" if word.endswith("token") else "疑似密钥"
    tail = value[len(value.rstrip("\"'")) :]
    return name + between + _count(kinds, label) + tail


def _url(match: re.Match[str], kinds: Counter) -> str:
    password = match.group(2)
    if not password or password.startswith(_PLACEHOLDER_START) or _FILLER.fullmatch(password):
        return match.group(0)
    return match.group(1) + _count(kinds, "网址里的密码") + match.group(3)

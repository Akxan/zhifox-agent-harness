"""语音转文字：聊天里发来的语音，先转成文字，再当作你说的话交给模型。

为什么要转：智狐用的这几家模型都只读文字和图片（Claude、GPT 的对话接口不收声音），而在手机上，按住说一句话
常常比打字快。所以收到一段语音时，智狐先把它交给一个"语音转文字"的服务，拿回文字，聊天里回一句
"（语音转文字）……"让你看看听得对不对，然后照常开始一轮。模型拿到的是带着"（语音转文字）"开头的文字，
知道这是听写出来的、可能有听错的字；网页上这段会话里，你的那条消息上有一个"语音转文字"的小标记。

能用哪几种（设置 → 语音转文字，见 settings.py）：

  auto     默认：填了 OpenAI 的 API 密钥就用 OpenAI，没填就当作关着
  openai   OpenAI 的转写接口（POST /v1/audio/transcriptions，默认模型 gpt-transcribe），按分钟计费
  local    你自己在这台电脑（或者局域网、你的服务器）上跑的、和 OpenAI 同一种接口的服务（比如 faster-whisper 的
           OpenAI 兼容服务器）。不花钱，OpenAI 的密钥绝不发给它。地址有两种给法：直接填一个；或者从"自定义模型接口"
           （endpoints/）里选一个本机或内网的接口，用它的地址和它自己的密钥（只发给它自己，和对话时一样）。
           外面的网关不在可选之列：它按分钟怎么收费智狐不知道，记账时没法算
  off      关着：收到语音时回一句怎么打开

花钱的事：OpenAI 按语音的长短收费（每分钟多少见 pricing.TRANSCRIBE_PRICES），每转一条在花费账本里记一笔，
来源是 voice（见 ledger.Ledger.record_audio）；本机的服务也记一笔，金额是 0。太长的语音（超过 config.VOICE_MAX_SECONDS）
不转，一分钱都不花，告诉你上限。

暂时只有 Telegram 会把语音交过来（channels/telegram.py 的 _audio）；别的平台收到语音时说"暂时只支持 Telegram"。

几个文件：
  settings.py    设置的样子和检查（地址、模型名）
  transcribe.py  选用哪一种（choose）、发出去转（transcribe）、转出来的结果和出错时给人看的话
  listen.py      聊天里收到一段语音时的整个过程：检查、下载、转、记账（channels/session.py 调用它）
"""

from .listen import listen
from .settings import DEFAULTS, PROVIDERS, check_model, check_url, voice_settings
from .transcribe import Heard, Transcriber, VoiceError, choose, transcribe, upload_name

__all__ = [
    "DEFAULTS",
    "PROVIDERS",
    "Heard",
    "Transcriber",
    "VoiceError",
    "check_model",
    "check_url",
    "choose",
    "listen",
    "transcribe",
    "upload_name",
    "voice_settings",
]

# 基于 ASR 的视频字幕提取与翻译 — 多厂商技术方案

> 版本：v1.0 | 日期：2026-06-08
> 适用：评估"视频→音频→ASR→翻译→字幕"路线，与已交付的《DEVELOPMENT.md》（OCR 路线）并存
> 目标：覆盖泰语、英语、阿拉伯语短剧

---

## 0. 可行性总评（TL;DR）

| 维度 | 结论 |
|------|------|
| **技术可行性** | ✅ 可行，但**单一火山引擎无法覆盖泰语和阿拉伯语**。需多厂商组合（火山 + 阿里 / Whisper）。 |
| **效率** | ASR 比 OCR 快 5~10 倍（无需逐帧处理），单视频处理时间可降到 20~60 秒（取决于异步轮询）。 |
| **成本** | 阿里云 ASR ≈ ¥0.20~¥0.30/分钟，Whisper ≈ $0.006/分钟（≈ ¥0.043），均显著低于 OCR 路线（OCR 路线约 ¥2.8/集 vs ASR 路线约 ¥0.4~¥1.0/集）。 |
| **质量** | **取决于音频质量**。短剧多有配乐+对白，ASR 准确率会受影响；硬字幕场景 OCR 更稳。 |
| **建议** | ASR 作为**主路线**，OCR 作为**回退路线**；按语种分流到不同厂商。 |

---

## 1. 技术路线全景图

```
┌─────────────────────────────────────────────────────────────────┐
│                       CLI / Batch Runner                        │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                ┌──────────▼──────────┐
                │  Pipeline Controller │
                └──────────┬──────────┘
                           │
       ┌───────────────────┴────────────────────┐
       │                                        │
┌──────▼──────┐                       ┌─────────▼─────────┐
│ SoftSubStage│                       │ AudioExtractor    │
│  (ffmpeg)   │                       │   (ffmpeg)        │
└──────┬──────┘                       └─────────┬─────────┘
       │ 有软字幕直接走                          │ 16kHz mono mp3
       │                                        │
       │                              ┌─────────▼─────────┐
       │                              │ TOS Uploader      │ ──▶ tos.volcengineapi.com
       │                              │  (可选远程URL)     │
       │                              └─────────┬─────────┘
       │                                        │
       │                          ┌─────────────┴─────────────┐
       │                          │      LangRouter           │
       │                          │ (按语种分发到不同ASR厂商)  │
       │                          └─┬─────────┬───────────┬───┘
       │                            │         │           │
       │              ┌─────────────▼──┐  ┌───▼────────┐ ┌▼──────────────┐
       │              │ VolcASR(zh/en) │  │ AliASR(th) │ │ Whisper(ar)   │
       │              │ openspeech.    │  │ nls-meta.  │ │ api.openai.com│
       │              │ bytedance.com  │  │ aliyuncs.. │ │               │
       │              └─────────────┬──┘  └───┬────────┘ └┬──────────────┘
       │                            └─────────┼───────────┘
       │                                      │
       │                            ┌─────────▼─────────┐
       │                            │ ASR→Cues Adapter  │
       │                            │ (统一时间戳格式)   │
       │                            └─────────┬─────────┘
       │                                      │
       │                            ┌─────────▼─────────┐
       │                            │ VolcTranslator    │ ──▶ translate.volcengineapi.com
       │                            │ (TranslateText)   │
       │                            └─────────┬─────────┘
       │                                      │
       └──────────────────────────────────────┴────▶ OutputWriter (SRT/VTT/TXT/双语)
```

---

## 2. 多厂商 ASR 对比表

> 数据来源：各厂商 2026 年 6 月公开文档。具体价格/QPS 以厂商控制台为准。

### 2.1 核心能力对比

| 厂商 | 泰语 (th) | 阿拉伯语 (ar) | 英语 (en) | 中文 (zh) | 时间戳粒度 | 异步/流式 | SRT 直出 |
|------|----------|---------------|-----------|-----------|------------|----------|----------|
| **火山引擎** 录音文件识别极速版 | ❌ 不支持 | ❌ 不支持 | ✅ en-US | ✅ zh-CN | 毫秒级（utterances + words） | 异步（submit/query） | 否（需手工转换） |
| **阿里云** 智能语音交互 NLS | ✅ "通用-泰语"模型 | ✅ "通用-阿拉伯语"模型 | ✅ "通用-英文" | ✅ 多种模型 | 句级（毫秒） | 异步 + 流式 | 否 |
| **腾讯云** ASR | ⚠️ 文档未明确 | ⚠️ 文档未明确 | ✅ | ✅ | 毫秒级 | 异步 + 流式 | 否 |
| **AWS Transcribe** | ✅ th-TH | ✅ ar-SA / ar-AE 等 | ✅ | ✅ | 字级 | 异步 + 流式 | ✅ 可直接输出 .srt |
| **Google Cloud STT** | ✅ th-TH | ✅ ar-XA + 多变体 | ✅ | ✅ | 字级 | 异步 + 流式 | 否（需转换） |
| **Azure Speech** | ✅ th-TH | ✅ ar-EG/ar-SA 等 22 种变体 | ✅ | ✅ | 字级 | 异步 + 流式 | 否 |
| **OpenAI Whisper** (whisper-1) | ✅ | ✅ | ✅ | ✅ | 字级 + 段级（verbose_json） | 同步（单文件 ≤25MB） | ✅ `response_format=srt` 直出 |

### 2.2 计费对比（公开价格，截至 2026-06；以官网最新为准）

| 厂商 | 计费方式 | 公开单价 | 折算 ¥/分钟 |
|------|---------|----------|-------------|
| 火山引擎 录音文件极速版 | 按秒计费 | ~¥0.40/小时 | ~¥0.007/分钟 |
| 阿里云 NLS 录音文件识别 | 按时长 | ¥1.40/小时 | ~¥0.023/分钟 |
| 腾讯云 录音文件识别 | 按时长 | ¥1.50/小时 | ~¥0.025/分钟 |
| AWS Transcribe Standard | 按秒 | $0.024/分钟 | ~¥0.17/分钟 |
| Google STT Standard | 按秒（≤60min/月免费） | $0.024/分钟 | ~¥0.17/分钟 |
| Azure Speech Standard | 按小时 | $1.00/小时 | ~¥0.12/分钟 |
| OpenAI Whisper API | 按分钟 | $0.006/分钟 | ~¥0.043/分钟 |

### 2.3 关键限制

| 厂商 | 单文件时长 | 单文件大小 | 默认 QPS/并发 |
|------|-----------|-----------|---------------|
| 火山引擎 极速版 | 5 小时 | 未明确 | 控制台查询 |
| 阿里云 NLS | 12 小时 | 512 MB | 默认 100 |
| 腾讯云 | 5 小时 | 1 GB | 默认 20 |
| AWS Transcribe | 4 小时 | 2 GB | 默认 100 |
| Google STT | 480 分钟（异步） | 1 GB | 默认 900/min |
| Azure Speech | 4 小时 | 1 GB | 默认 100 |
| OpenAI Whisper | 无明确 | **25 MB** | 默认 50 RPM |

### 2.4 鉴权与 SDK

| 厂商 | 鉴权 | Python SDK 包名 |
|------|------|-----------------|
| 火山引擎 | AppID + Token（请求体）/ AK-SK 签名 V4 | `volcengine` |
| 阿里云 | AK + AccessKeySecret + AppKey | `alibabacloud-nls-python-sdk` / `aliyun-python-sdk-nls` |
| 腾讯云 | SecretId + SecretKey | `tencentcloud-sdk-python-asr` |
| AWS | Access Key + Secret Key + Region | `boto3` |
| Google | Service Account JSON | `google-cloud-speech` |
| Azure | Subscription Key + Region | `azure-cognitiveservices-speech` |
| OpenAI | API Key | `openai` |

---

## 3. 推荐组合方案

### 3.1 总原则

> **按语种分流，按成本择优；火山做翻译，多家做识别。**

### 3.2 推荐组合：三厂商分工

```
┌─────────────────────────────────────────────────────────────┐
│  语言路由（LangRouter）— 优先级从上到下                       │
├─────────────────────────────────────────────────────────────┤
│  zh-CN / en-US / ja-JP / ko-KR  →  火山引擎 ASR（最便宜）    │
│  th-TH（泰语）                   →  阿里云 ASR（覆盖好）      │
│  ar-XX（阿拉伯语）               →  阿里云 ASR 或 Whisper    │
│  其他/未知语种                   →  OpenAI Whisper（兜底）   │
│                                                              │
│  所有原文 → 火山引擎机器翻译 → 中文                          │
└─────────────────────────────────────────────────────────────┘
```

### 3.3 备选组合（单厂商优先）

| 场景 | 推荐 | 理由 |
|------|------|------|
| 国内合规优先、批量处理 | 阿里云 NLS（全程） + 火山翻译 | 阿里覆盖泰阿，国内备案合规 |
| 海外项目、英语为主 | OpenAI Whisper + DeepL/Google 翻译 | Whisper 一站式，质量稳定 |
| 极致省钱 | 火山引擎 ASR（仅 zh/en）+ Whisper（其他） + 火山翻译 | 复用已有 AK/SK，控制总成本 |

### 3.4 不推荐的方案

- ❌ **腾讯云全程**：文档中泰语/阿语支持不明确
- ❌ **AWS/Google 全程**：海外服务延迟高，国内项目不便
- ❌ **Azure 全程**：定价偏高且国内访问需特殊网络

---

## 4. 音频提取模块（ffmpeg）

### 4.1 推荐参数

ASR 服务通常推荐 **16 kHz 单声道**，且压缩格式比 PCM 节省传输带宽。

```bash
# 主推：16 kHz, 单声道, MP3 64kbps（兼顾质量与体积）
ffmpeg -i input.mp4 -vn -ac 1 -ar 16000 -b:a 64k -f mp3 output.mp3

# 备选：原始 PCM（无损但体积大，10 分钟 ≈ 18 MB）
ffmpeg -i input.mp4 -vn -ac 1 -ar 16000 -f s16le -acodec pcm_s16le output.pcm
```

### 4.2 参数说明

| 参数 | 含义 | 推荐值 |
|------|------|--------|
| `-vn` | 去掉视频流 | 必填 |
| `-ac 1` | 单声道 | 必填（ASR 多为单声道） |
| `-ar 16000` | 采样率 16 kHz | 必填（ASR 推荐） |
| `-b:a 64k` | 音频码率 64 kbps | 推荐（人声足够） |
| `-f mp3` | 输出格式 | 推荐（兼容性最好） |

### 4.3 体积估算

| 视频时长 | MP3 (16k mono 64kbps) | PCM (16k mono 16bit) |
|---------|----------------------|----------------------|
| 1 分钟 | ~480 KB | ~1.9 MB |
| 10 分钟 | ~4.8 MB | ~19 MB |
| 1 小时 | ~28 MB | ~115 MB |

> Whisper 限制 25MB → 单次最多约 50 分钟 MP3，超过需切片。

### 4.4 模块接口（`pipeline/audio.py`）

```python
class AudioExtractor:
    def extract(self, video_path: Path,
                output_path: Path,
                sample_rate: int = 16000,
                channels: int = 1,
                format: str = "mp3",
                bitrate: str = "64k") -> Path:
        """从视频提取音频，返回音频文件路径。"""

    def split_by_size(self, audio_path: Path,
                       max_mb: int = 24) -> list[Path]:
        """按大小切分音频（用于 Whisper 25MB 限制）。"""

    def get_duration(self, audio_path: Path) -> float:
        """返回音频时长（秒）。"""
```

---

## 5. TOS 上传模块（可选）

火山引擎录音文件识别**必须使用 audio_url 而非 base64**。需把音频上传到能够公网访问的对象存储。

### 5.1 选项

| 选项 | 优点 | 缺点 |
|------|------|------|
| **火山 TOS** | 同厂商，签名URL便利 | 需开通 TOS 服务 |
| **阿里 OSS** | 国内速度快 | 跨厂商 |
| **自建 nginx 临时托管** | 零成本 | 需公网 IP，安全风险 |
| **隧道（如 ngrok）** | 开发期快 | 不适合生产 |

### 5.2 推荐：火山 TOS

```python
class TosUploader:
    def __init__(self, ak: str, sk: str,
                 endpoint: str = "tos-cn-beijing.volces.com",
                 bucket: str = "subtitle-asr"): ...

    def upload(self, local_path: Path,
               object_key: str | None = None,
               ttl_sec: int = 3600) -> str:
        """上传文件返回有效 1 小时的签名 URL。"""

    def delete(self, object_key: str) -> None:
        """ASR 完成后清理，避免占用空间。"""
```

调用流程：

```
extract → upload to TOS → get signed url → submit ASR with url
                                              ↓
                                          poll query
                                              ↓
                                        delete TOS object
```

### 5.3 SDK

- 包名：`tos`（火山 TOS SDK）
- 安装：`pip install tos`

---

## 6. 核心 ASR 模块设计

### 6.1 统一接口

为屏蔽多厂商差异，定义统一的 ASR 客户端协议：

```python
@dataclass
class ASRWord:
    text: str
    start_ms: int
    end_ms: int

@dataclass
class ASRUtterance:
    text: str
    start_ms: int
    end_ms: int
    lang: str
    words: list[ASRWord]

@dataclass
class ASRResult:
    utterances: list[ASRUtterance]
    detected_lang: str
    duration_sec: float
    raw: dict        # 保留厂商原始返回

class ASRClient(Protocol):
    def recognize(self, audio_input: AudioInput,
                  language: str | None = None) -> ASRResult: ...
```

### 6.2 火山引擎客户端（`pipeline/asr_volc.py`）

```python
class VolcASRClient:
    SUBMIT_URL = "https://openspeech.bytedance.com/api/v1/auc/submit"
    QUERY_URL  = "https://openspeech.bytedance.com/api/v1/auc/query"

    def __init__(self, appid: str, token: str, cluster: str,
                 poll_interval: float = 2.0,
                 poll_timeout: float = 600.0): ...
```

**请求体（submit）：**

```json
{
  "app": { "appid": "...", "token": "...", "cluster": "volc_auc_common" },
  "user": { "uid": "subtitle-extractor" },
  "audio": {
    "url": "https://tos-cn-beijing.volces.com/.../ep01.mp3",
    "format": "mp3",
    "codec": "raw",
    "rate": 16000,
    "bits": 16,
    "channel": 1,
    "language": "en-US"
  },
  "additions": { "use_itn": "true", "use_punc": "true" }
}
```

**响应（query）含 utterances：**

```json
{
  "resp": {
    "code": 1000,
    "utterances": [
      {
        "text": "Hello world",
        "start_time": 1200,
        "end_time": 3500,
        "words": [
          { "text": "Hello", "start_time": 1200, "end_time": 1800 },
          { "text": "world", "start_time": 2000, "end_time": 3500 }
        ]
      }
    ]
  }
}
```

**支持语言（重要）：** zh-CN, en-US, ja-JP, ko-KR, fr-FR, es-MX, pt-BR, id-ID。**不支持泰语、阿拉伯语。**

### 6.3 阿里云客户端（`pipeline/asr_ali.py`）

```python
class AliASRClient:
    """阿里云智能语音交互 - 录音文件识别"""
    BASE_URL = "https://nls-filetrans.cn-shanghai.aliyuncs.com"

    def __init__(self, access_key_id: str,
                 access_key_secret: str,
                 app_key: str,
                 region: str = "cn-shanghai"): ...
```

**关键参数：**

- `model`（模型）：
  - 泰语：通用-泰语模型
  - 阿语：通用-阿拉伯语模型
  - 英语：通用-英文模型
- `format`: mp3 / wav / pcm
- `sample_rate`: 16000
- `enable_words`: true（要求返回字级时间戳）

**返回结构：**

```json
{
  "Result": {
    "Sentences": [
      { "Text": "...", "BeginTime": 1200, "EndTime": 3500, "ChannelId": 0 }
    ]
  }
}
```

### 6.4 OpenAI Whisper 客户端（`pipeline/asr_whisper.py`）

```python
class WhisperClient:
    def __init__(self, api_key: str,
                 model: str = "whisper-1",
                 max_size_mb: int = 24): ...

    def recognize(self, audio_path: Path,
                  language: str | None = None) -> ASRResult:
        """同步调用，超过 max_size_mb 自动切片串行送翻"""
```

**优势：**
- 一行代码即可直出 SRT：`response_format="srt"`
- 99 种语言全覆盖，自动语种检测
- 价格低（$0.006/分钟）

**限制：**
- 单文件 ≤25 MB（必须分片）
- 海外服务，国内需代理
- 同步调用，无异步 API

**调用示例（伪代码）：**

```python
from openai import OpenAI
client = OpenAI(api_key="...")
with open("audio.mp3", "rb") as f:
    resp = client.audio.transcriptions.create(
        model="whisper-1",
        file=f,
        response_format="verbose_json",
        timestamp_granularities=["word", "segment"],
        language="th",  # 可选
    )
# resp.segments → list of {id, start, end, text}
```

### 6.5 LangRouter（语言路由）

```python
class LangRouter:
    def __init__(self, volc: VolcASRClient | None,
                 ali: AliASRClient | None,
                 whisper: WhisperClient | None,
                 strategy: str = "cost"): ...

    def route(self, lang_hint: str | None) -> ASRClient:
        """根据语种和策略选择客户端"""
```

**策略：**
- `strategy="cost"`：火山 > 阿里 > Whisper（按价格优先）
- `strategy="quality"`：Whisper > 阿里 > 火山（按业界口碑）
- `strategy="local"`：阿里 > 火山 > Whisper（避免海外服务）

---

## 7. 翻译模块（复用 OCR 路线）

复用 `DEVELOPMENT.md` 第 5.2 节定义的 `VolcTranslator`。

**唯一差异：** 输入从"OCR 出来的字幕短文本"变成"ASR 出来的整句话"。
- 单条字符数通常 5~50（短剧台词），仍在 4500 字符上限内。
- 翻译缓存依然有效，短剧台词重复率高。

---

## 8. 时间戳 → SRT 转换

### 8.1 各厂商时间戳字段映射

| 厂商 | 句级开始字段 | 句级结束字段 | 单位 |
|------|-------------|-------------|------|
| 火山 | `utterances[].start_time` | `utterances[].end_time` | 毫秒 |
| 阿里 | `Sentences[].BeginTime` | `Sentences[].EndTime` | 毫秒 |
| Whisper | `segments[].start` | `segments[].end` | 秒（float） |
| Azure | `Offset`（100纳秒） | `Duration` | 100纳秒（除以10000得毫秒） |

### 8.2 统一适配器

```python
def to_subtitle_cues(asr_result: ASRResult,
                      max_chars_per_line: int = 30,
                      max_duration_sec: float = 6.0) -> list[SubtitleCue]:
    """ASRResult → SubtitleCue 列表，复用 OCR 路线的 SubtitleCue 模型"""
```

**关键处理：**
1. **过长句子切分**：单条 utterance > max_duration_sec 时，按 words 切分。
2. **过短句子合并**：相邻 < 0.3s 间隔的句子合并（避免字幕跳动）。
3. **时间戳取整**：转毫秒，避免浮点误差导致 SRT 时间错位。
4. **去除填充词**："呃""um" 等（可选过滤列表）。

### 8.3 SRT 写入复用 OCR 路线的 `write_srt()`

---

## 9. CLI 设计

在现有 `extract_subtitles.py` 上新增以下参数：

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `--engine` | str | `ocr` | `ocr` / `asr` / `auto`（auto = 优先ASR，失败回退OCR） |
| `--asr-vendor` | str | `auto` | `volc` / `ali` / `whisper` / `auto`（按 lang 路由） |
| `--asr-strategy` | str | `cost` | `cost` / `quality` / `local` |
| `--audio-format` | str | `mp3` | ffmpeg 输出格式 |
| `--audio-bitrate` | str | `64k` | ffmpeg 音频码率 |
| `--use-tos` | flag | False | 上传到 TOS 拿签名 URL（火山 ASR 必需） |
| `--tos-bucket` | str | env | TOS bucket 名 |
| `--ali-ak` / `--ali-sk` / `--ali-appkey` | str | env | 阿里云凭据 |
| `--openai-api-key` | str | env | OpenAI API Key |
| `--whisper-max-mb` | int | 24 | 切片阈值 |
| `--asr-poll-interval` | float | 2.0 | 火山/阿里异步轮询间隔（秒） |
| `--asr-poll-timeout` | float | 600 | 异步任务超时（秒） |

**使用示例：**

```bash
# ASR 路线，全自动按语种路由
python extract_subtitles.py "./videos/*.mp4" --engine asr --asr-vendor auto --translate

# 指定 Whisper 兜底，输出双语
python extract_subtitles.py "./videos/*.mp4" --engine asr --asr-vendor whisper --translate --bilingual

# 阿里云专做泰语
python extract_subtitles.py "./videos/*.thai.mp4" --engine asr --asr-vendor ali --source-lang th --translate

# 混合：ASR 失败回退 OCR
python extract_subtitles.py "./videos/*.mp4" --engine auto --translate
```

---

## 10. ASR vs OCR 对比

| 维度 | ASR 路线 | OCR 路线 |
|------|---------|----------|
| **核心输入** | 音频流 | 视频帧 |
| **依赖** | ffmpeg + ASR API + (TOS) | ffmpeg + OCR API |
| **典型耗时（90s 短剧）** | 20~60s（异步轮询） | 70s（5 QPS）/ 42s（20 QPS） |
| **典型成本** | ¥0.05~¥0.30/集 | ¥2.80/集 |
| **多语种支持** | 受厂商限制（火山不支持泰阿） | OCR 厂商 50+ 语种全覆盖 |
| **准确率** | 取决于音频质量（配乐/背景音降低准确率） | 取决于字幕清晰度（水印、字号、颜色） |
| **配音剧（无字幕）** | ✅ 唯一选项 | ❌ 无法工作 |
| **无配音（仅字幕）** | ❌ 无法工作 | ✅ 唯一选项 |
| **有配音 + 有字幕** | 两者都可用，看哪个准 | 两者都可用 |
| **时间戳精度** | 极高（ASR 自带精确时间戳） | 受采样间隔限制（0.5s 粒度） |
| **错别字风险** | 谐音字、专有名词易错 | 形似字、低对比度易错 |
| **海外/方言剧** | Whisper 表现好 | OCR 依然稳定 |
| **预处理复杂度** | 简单（ffmpeg 一行） | 中等（ROI 裁剪 + 增强） |
| **首次集成成本** | 中（多厂商 SDK） | 低（火山单厂商） |

---

## 11. 混合策略（ASR + OCR 互补）

### 11.1 何时需要混合

短剧有四类典型场景：

| 场景 | 配音 | 字幕 | 推荐 |
|------|------|------|------|
| A. 原声 + 硬字幕（最常见） | ✅ | ✅ | ASR（精准时间戳）+ OCR（兜底校正） |
| B. 仅原声无字幕 | ✅ | ❌ | 必须 ASR |
| C. 仅硬字幕（静音剧/配乐剧） | ❌ | ✅ | 必须 OCR |
| D. 配音 ≠ 字幕（如英语原声 + 中文字幕） | ✅ | ✅ | 用户选择：ASR 走原声转译，OCR 直读字幕 |

### 11.2 自动决策流程

```
START
  │
  ├─▶ ffmpeg 提取音频 + 测算音频能量
  │     ├─ 平均音量 < -50 dBFS → 静音/配乐剧 → 走 OCR
  │     └─ 否则继续
  │
  ├─▶ 抽 1~2 帧测 ROI 是否有文字（轻量 OCR）
  │     ├─ 有文字且置信度高 → 候选 OCR
  │     └─ 无文字 → 走 ASR
  │
  ├─▶ 跑 ASR（短试一段 30s）
  │     ├─ 返回有效 utterances 且平均置信度 > 0.7 → 走 ASR 全量
  │     └─ 返回空或低置信 → 回退 OCR
  │
  └─▶ 出结果
```

### 11.3 双轨并行 + 仲裁（高级）

对质量要求高的视频，可同时跑 ASR 和 OCR，按规则合并：

- 时间戳一致段 → 选 ASR 文本（更精确）
- 仅 OCR 有文本 → 用 OCR（弥补 ASR 漏识别）
- 仅 ASR 有文本 → 用 ASR（背景人物对白）
- 两者文本差异大 → 标记低置信，写入 `_conflicts.json` 供人工复核

成本翻倍，但质量提升明显。

---

## 12. 成本估算（单视频 90 秒短剧）

### 12.1 单视频用量

| 项目 | 数量 |
|------|------|
| 视频时长 | 1.5 分钟 |
| 音频提取 | 1 次 ffmpeg（本地，免费） |
| ASR 调用 | 1 次（整段送翻） |
| 翻译字符数 | 约 1500 字（60 条 cue × 25 字/条） |
| 翻译批次 | 4 次（16 条/批） |

### 12.2 单视频成本对比

| 厂商组合 | ASR 成本 | 翻译成本 | 合计 ¥/集 |
|---------|----------|----------|-----------|
| 火山 ASR（zh/en） + 火山翻译 | 1.5min × ¥0.007 = ¥0.011 | ¥0.07 | **¥0.08** |
| 阿里 ASR（泰/阿） + 火山翻译 | 1.5min × ¥0.023 = ¥0.035 | ¥0.07 | **¥0.11** |
| Whisper（任意） + 火山翻译 | 1.5min × ¥0.043 = ¥0.065 | ¥0.07 | **¥0.14** |
| **对照：OCR 路线**（DEVELOPMENT.md） | OCR ¥2.70 | ¥0.07 | **¥2.77** |

> ASR 路线比 OCR 路线便宜 **20~35 倍**。

### 12.3 批量估算（每日 100 集短剧）

| 方案 | 月成本（30天） |
|------|----------------|
| 火山 ASR 全量 | ¥240 |
| 阿里 ASR 全量 | ¥330 |
| Whisper 全量 | ¥420 |
| OCR 路线 | ¥8,310 |

---

## 13. 边界与限制

### 13.1 ASR 通用限制

1. **音频质量决定上限**：背景配乐、多人对白、口音重 → 准确率显著下降。
2. **专有名词错字多**：演员名、剧名、地名常被识别为同音字。可用厂商的"热词/术语表"功能改善。
3. **静音剧/纯字幕剧**：ASR 完全无法工作，必须回退 OCR。
4. **时间戳偏移**：ASR 返回的是说话开始时间，可能与字幕显示时间错位约 0.2~0.5 秒。
5. **大文件切片**：Whisper 25 MB / 阿里 512 MB 上限，长视频需切片后拼接（拼接时注意累计时间戳偏移）。

### 13.2 厂商专有限制

| 厂商 | 注意事项 |
|------|---------|
| 火山 | 需要 audio_url（必须先上传 TOS）；语种范围窄 |
| 阿里 | 不同语种用不同模型，需切换 model 参数 |
| Whisper | 单文件 25MB；同步调用，长视频耗时；海外网络需代理 |
| 全部 | 异步任务建议轮询，避免长轮询超时 |

### 13.3 需控制台核实的项

| 项 | 原因 |
|----|------|
| 火山 ASR 极速版具体单价 | 文档未列详细价表 |
| 火山 ASR 是否支持 base64 上传 | 文档主推 audio_url |
| 阿里云 NLS 各模型 appkey 申请流程 | 不同语种可能需要单独申请 |
| 各厂商 QPS 上限 | 默认值需在控制台申请提升 |
| Whisper 国内访问稳定性 | 取决于代理网络质量 |

### 13.4 安全与合规

1. **音频也包含个人信息**：上传至云端 ASR 需评估是否涉及版权/隐私。
2. **TOS/OSS 签名 URL** 应设置短 TTL（如 1 小时），避免长期泄漏。
3. **多 AK 隔离**：火山、阿里、OpenAI 的密钥应分别用环境变量管理，禁止混在同一配置文件。
4. **审计日志**：ASR 调用频次高，建议记录 `request_id` + 时长 + 成本到本地日志。

---

## 14. 实施建议

### 14.1 优先级

| 阶段 | 内容 | 预期交付时间 |
|------|------|--------------|
| Phase 1 | 火山 ASR + TOS 上传 + 翻译（zh/en 验证） | 3 天 |
| Phase 2 | Whisper 集成（兜底全语种） | 2 天 |
| Phase 3 | 阿里 ASR 集成（泰/阿专项） | 3 天 |
| Phase 4 | LangRouter + 混合策略 + report | 2 天 |
| Phase 5 | ASR+OCR 双轨仲裁（可选） | 3 天 |

### 14.2 与 OCR 路线的关系

- 共用：`softsub.py`、`merger.py`、`writer.py`、`config.py`、`translator_volc.py`
- 新增：`audio.py`、`tos_uploader.py`、`asr_volc.py`、`asr_ali.py`、`asr_whisper.py`、`lang_router.py`
- CLI 通过 `--engine {ocr,asr,auto}` 切换

### 14.3 验收标准

| 阶段 | 验收 |
|------|------|
| Phase 1 | 90s 英语短剧 ASR 出 SRT，准确率 > 90% |
| Phase 2 | 同一视频 Whisper 路径出 SRT，与火山输出相近 |
| Phase 3 | 90s 泰语短剧阿里 ASR 出 SRT，准确率 > 85% |
| Phase 4 | 三语混测，自动路由全部成功，成本 < ¥0.15/集 |
| Phase 5 | 双轨模式发现 ASR/OCR 冲突时 `_conflicts.json` 非空 |

---

## 15. 总结对比表（与 OCR 路线对照）

| 维度 | OCR 路线（DEVELOPMENT.md） | ASR 路线（本文） |
|------|--------------------------|------------------|
| 单厂商可行 | ✅ 仅需火山 | ❌ 需多厂商组合 |
| 语种覆盖 | 50+ 语种（火山 OCR） | 需厂商分流 |
| 单视频成本 | ¥2.80/集 | ¥0.08~¥0.14/集 |
| 单视频耗时 | 42~70s | 20~60s |
| 时间戳精度 | 采样间隔（默认 0.5s） | 毫秒级 |
| 静音剧 | ✅ 可处理 | ❌ 失败 |
| 配音无字幕剧 | ❌ 失败 | ✅ 可处理 |
| 集成复杂度 | 低（单厂商） | 中（多厂商 + TOS） |
| 推荐用法 | 兜底/纯字幕剧 | 主路线/配音剧 |

**建议：** 两条路线**并行实施**，由 `--engine auto` 自动决策；批处理时按视频类型分桶处理。

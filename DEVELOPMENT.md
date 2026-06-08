# 火山引擎云端化字幕提取与翻译 — 开发文档

> 版本：v1.0 | 日期：2026-06-08
> 适用：将 `extract_subtitles.py` 从本地 PaddleOCR 改造为基于火山引擎云端 OCR + 翻译的多语种字幕提取/翻译流水线。
> 原则：零本地模型，全部走云端 API；输出原文 + 中文 + 双语三套字幕；语种自动检测，不限语种。

---

## 1. 系统架构概览

### 1.1 设计目标

- **零本地大模型**：去除 PaddlePaddle / PaddleOCR，体积减半、启动秒级。
- **多语种支持**：覆盖泰语 (th)、英语 (en)、阿拉伯语 (ar) 等 50+ 语种，OCR 自动识别语种。
- **OCR + 翻译一体化**：输入外文短剧 → 输出中文 SRT/VTT/TXT，可选双语版本。
- **保留确定性 pipeline**：ffmpeg 软字幕 + 帧采样 + ROI 裁剪 + 时序合并的核心逻辑不变。
- **可观测、可控成本**：API 调用次数、失败率可统计，每视频生成 report。

### 1.2 架构图

```
┌─────────────────────────────────────────────────────────────┐
│                     CLI (extract_subtitles.py)               │
└──────────────────────────┬──────────────────────────────────┘
                           │
              ┌────────────▼────────────┐
              │  Pipeline Controller    │
              └────────────┬────────────┘
       ┌───────────────────┼───────────────────────┐
       │                   │                       │
┌──────▼──────┐   ┌────────▼────────┐   ┌─────────▼─────────┐
│ SoftSubStage│   │  FrameSampler   │   │   OutputWriter    │
│ (ffmpeg)    │   │ (OpenCV + ROI)  │   │ (SRT/VTT/TXT/双语)│
└──────┬──────┘   └────────┬────────┘   └───────────────────┘
       │                   │
       │          ┌────────▼────────┐
       │          │ VolcOCRClient   │ ──HTTPS──▶ visual.volcengineapi.com
       │          │(MultiLanguageOCR)│            Action=MultiLanguageOCR
       │          └────────┬────────┘
       │                   │
       │          ┌────────▼────────┐
       │          │   CueMerger     │
       │          │(相似度+时序合并) │
       │          └────────┬────────┘
       │                   │
       │          ┌────────▼────────┐
       │          │ VolcTranslator  │ ──HTTPS──▶ translate.volcengineapi.com
       │          │(TranslateText)  │            Action=TranslateText
       │          └────────┬────────┘
       │                   │
       └───────────────────┴────▶ cues: original_text + zh_text
```

### 1.3 目录结构

```
extract_subtitle/
├── extract_subtitles.py        # 入口与 CLI（薄入口，调用 pipeline）
├── pipeline/
│   ├── __init__.py
│   ├── config.py               # 配置加载、AK/SK、环境变量
│   ├── models.py               # SubtitleCue / OCRSample / OCRBox 等 dataclass
│   ├── softsub.py              # ffprobe / ffmpeg 软字幕提取（迁移原逻辑）
│   ├── sampler.py              # OpenCV 抽帧、ROI 裁剪、预处理
│   ├── ocr_volc.py             # 火山 OCR 客户端 + 签名 + 重试 + QPS 限速
│   ├── translator_volc.py      # 火山翻译客户端 + 批处理 + 缓存
│   ├── merger.py               # 采样去重、相似度合并、postprocess
│   └── writer.py               # SRT/VTT/TXT/双语输出
├── requirements.txt
├── DEVELOPMENT.md              # 本文档
├── CLAUDE.md
└── README.md
```

---

## 2. API 服务选型清单

| # | 能力 | 服务名 | Action | Host | 版本 | 计费 | 默认QPS | 用途 |
|---|------|--------|--------|------|------|------|---------|------|
| 1 | 多语种 OCR | 视觉智能 / 通用文字识别 | `MultiLanguageOCR` | `visual.volcengineapi.com` | `2022-08-31` | 按调用次数；新用户 5000次/月免费 | 5（可申请提升） | 识别帧中外文字幕 |
| 2 | 机器翻译 | translate | `TranslateText` | `translate.volcengineapi.com` | `2020-06-01` | 按字符数计费 | 5 | 外文→中文翻译 |
| 3 | 语种检测（可选） | translate | `LangDetect` | `translate.volcengineapi.com` | `2020-06-01` | 按调用次数 | 5 | OCR 未返回 lang 时兜底 |

本地工具（不计费）：

| # | 能力 | 工具 | 用途 |
|---|------|------|------|
| 4 | 音视频处理 | FFmpeg / FFprobe | 软字幕抽取、容器探测 |
| 5 | 视频解码/抽帧 | OpenCV (cv2) | 按 interval 抽帧 |

> 价格以火山引擎官网实时为准，本文档仅做架构占位。

---

## 3. 火山引擎账号准备

### 3.1 注册与实名

1. 访问 https://www.volcengine.com/ ，使用手机号注册账号。
2. 完成「企业实名认证」（个人也可，但部分配额受限）。
3. 进入「访问控制 IAM」→「访问密钥」→ 创建 AccessKey，记录 `AccessKeyId` 和 `SecretAccessKey`。**SecretAccessKey 仅创建时可见一次，请立即保存。**

### 3.2 开通所需服务

1. 搜索「**通用文字识别**」/「**视觉智能**」→「立即开通」→ 确认 `MultiLanguageOCR` 已开通。
2. 搜索「**机器翻译**」→ 开通服务，选购或领取免费资源包。
3. （推荐）为账户充值或购买预付费资源包，避免突发限频。

### 3.3 配置环境

环境变量（推荐）：

```bash
export VOLC_ACCESS_KEY_ID="AKLT..."
export VOLC_SECRET_ACCESS_KEY="..."
export VOLC_REGION="cn-north-1"
```

或使用项目根目录 `.env` 文件（git 忽略），通过 `python-dotenv` 加载：

```
VOLC_ACCESS_KEY_ID=AKLT...
VOLC_SECRET_ACCESS_KEY=...
VOLC_REGION=cn-north-1
```

验证连通性：用 curl 或 SDK 调一次 `TranslateText`，返回 `200 + TranslationList` 即可。

### 3.4 配额提升

- 默认 QPS=5。短剧批处理建议在控制台提交「QPS 提升」工单，目标 20~50 QPS。
- OCR 单图 Base64 后限制 8MB，ROI 裁剪后通常 <200KB，无需担心。

---

## 4. 处理流程（Pipeline）

```
START
  │
  ├─▶ [1] ensure_tools()        # 检查 ffmpeg/ffprobe
  ├─▶ [2] load_config()         # 读 AK/SK、目标语言、translate 开关
  ├─▶ [3] init_clients()        # VolcOCRClient + VolcTranslator（单例复用）
  │
  └─▶ for each video:
        │
        ├─▶ [4] try_extract_soft_subtitle()
        │        ├─ True  ─▶ read_srt_text() ─▶ [10] translate (if enabled)
        │        └─ False ─▶ continue
        │
        ├─▶ [5] sample_video_ocr_cloud():
        │        for t in 0..duration step interval:
        │           frame = cap.read(t)
        │           roi   = crop_subtitle_roi(frame, ...)
        │           prep  = preprocess_for_ocr(roi)
        │           jpg   = cv2.imencode('.jpg', prep) → base64
        │           resp  = ocr_client.recognize(jpg_bytes)
        │           text, lang, score = pick_subtitle_line(resp)
        │           if looks_like_dialogue_multilang(text, lang):
        │              samples.append(OCRSample(t, text, score, lang))
        │
        ├─▶ [6] merge_samples_to_cues()     # 复用原逻辑（阈值按语言调整）
        ├─▶ [7] postprocess_cues()
        │
        ├─▶ [8] detect dominant language     # 取众数 lang
        │
        ├─▶ [9] write 原文字幕 SRT/VTT/TXT
        │
        ├─▶ [10] if translate_enabled and lang != "zh":
        │          batch_translate(cues, src=lang, tgt="zh")
        │          write 中文字幕 SRT/VTT/TXT
        │          write 双语字幕 SRT
        │
        └─▶ [11] write report.json (调用次数/成本/耗时)
END
```

---

## 5. 核心模块设计

### 5.1 OCR 模块（`pipeline/ocr_volc.py`）

#### 5.1.1 客户端接口

```python
class VolcOCRClient:
    def __init__(self, ak: str, sk: str,
                 host: str = "visual.volcengineapi.com",
                 region: str = "cn-north-1",
                 timeout: float = 10.0,
                 max_retries: int = 3,
                 backoff: float = 0.8,
                 qps_limit: float = 5.0): ...

    def recognize(self, image_bytes: bytes,
                  mode: str = "text_block",
                  filter_thresh: int = 80,
                  approximate_pixel: int = 4) -> OCRResult: ...
```

#### 5.1.2 请求构造

- HTTP：`POST https://visual.volcengineapi.com/?Action=MultiLanguageOCR&Version=2022-08-31`
- Header：
  - `Content-Type: application/x-www-form-urlencoded`
  - `Host: visual.volcengineapi.com`
  - `X-Date: 20260608T123456Z`
  - `Authorization: HMAC-SHA256 Credential=..., SignedHeaders=..., Signature=...`
- Body（form-urlencoded）：
  - `image_base64` = Base64(jpeg_bytes)
  - `mode=text_block`
  - `filter_thresh=80`
  - `approximate_pixel=4`

> **推荐使用官方 SDK**：`from volcengine.visual.VisualService import VisualService`，调用 `visual.multi_language_ocr(form)`，自动处理签名 V4，避免自行实现 HMAC-SHA256。

#### 5.1.3 响应数据结构

火山原始返回：

```json
{
  "code": 10000,
  "message": "Success",
  "request_id": "...",
  "data": {
    "ocr_infos": [
      { "lang": "th", "text": "สวัสดี", "rect": {...}, "prob": 0.97 }
    ]
  }
}
```

数据模型（`pipeline/models.py`）：

```python
@dataclass
class OCRBox:
    text: str
    lang: str        # th / en / ar / zh ...
    prob: float      # 0~1
    rect: tuple      # (x1, y1, x2, y2)

@dataclass
class OCRResult:
    raw: dict
    boxes: list[OCRBox]
    request_id: str
    cost_ms: int

    def merged_text(self, min_prob: float = 0.6) -> tuple[str, str, float]:
        """合并所有 box → (text, dominant_lang, mean_prob)"""
```

#### 5.1.4 字幕行选择策略

- `mode=text_block` 会把字幕合并成一行/一块，优先使用。
- 当返回多个 `ocr_infos`：
  1. 过滤 `prob < min_prob`（默认 0.6）；
  2. 优先选 **rect 在画面下半部** 的 box（y_center > 0.5）；
  3. 同一 ROI 内多行字幕，按 `y_center` 升序合并；
  4. 取出现频次最高的 `lang` 作为该样本的主语种。

#### 5.1.5 错误处理与重试

| 错误类型 | code 示例 | 处理 |
|---------|-----------|------|
| 限流 / QPS 超限 | 50429 / RateLimited | 指数退避：sleep = backoff × 2^n，最多 max_retries 次 |
| 签名错误 | 100009 | 立即抛出，不重试（配置错） |
| 图片过大 / 无效 | 18002 | 记录后跳过该帧，不重试 |
| 网络超时 | requests.Timeout | 重试，timeout 阶梯放大 |
| 服务端 5xx | code >= 50000 | 重试 |

- 失败超过 max_retries 的帧：写入 `failed_frames.json`，继续下一帧，不中断整体流程。
- 全局 QPS 限速：基于 token bucket（`time.monotonic()` + `threading.Lock`），保证 ≤ qps_limit。

#### 5.1.6 关键参数表

| 参数 | 默认 | 说明 |
|------|------|------|
| `mode` | `text_block` | 字幕一般是连续一行/两行，文本块模式更合适 |
| `filter_thresh` | `80` | 服务端置信度过滤（0~100） |
| `approximate_pixel` | `4` | 行高合并阈值，字幕字号不大可以小一点 |
| `min_prob`（客户端） | `0.6` | 客户端二次过滤 |
| `qps_limit` | `5` | 与账户配额一致 |
| `max_retries` | `3` | 退避基数 0.8s |
| `image_format` | `JPEG, q=85` | 比 PNG 小，传输快 |
| `max_image_side` | `1920` | 超过则等比缩小，控制单图体积 |

---

### 5.2 翻译模块（`pipeline/translator_volc.py`）

#### 5.2.1 客户端接口

```python
class VolcTranslator:
    def __init__(self, ak: str, sk: str,
                 host: str = "translate.volcengineapi.com",
                 region: str = "cn-north-1",
                 timeout: float = 10.0,
                 max_retries: int = 3,
                 qps_limit: float = 5.0,
                 max_batch_items: int = 16,
                 max_batch_chars: int = 4500): ...

    def translate(self, texts: list[str],
                  target: str = "zh",
                  source: str | None = None) -> list[TranslatedItem]: ...
```

#### 5.2.2 请求构造

- HTTP：`POST https://translate.volcengineapi.com/?Action=TranslateText&Version=2020-06-01`
- Header：JSON + 签名 V4（Service=`translate`, Region=`cn-north-1`）。
- Body：

```json
{
  "TargetLanguage": "zh",
  "SourceLanguage": "th",
  "TextList": ["...", "..."]
}
```

- 推荐用 SDK：`from volcengine.ApiInfo import ApiInfo` + Service 工具类。

#### 5.2.3 响应数据结构

火山原始返回：

```json
{
  "ResponseMetadata": { "RequestId": "..." },
  "TranslationList": [
    { "Translation": "你好", "DetectedSourceLanguage": "th" }
  ]
}
```

数据模型：

```python
@dataclass
class TranslatedItem:
    src_text: str
    dst_text: str
    src_lang: str
    cost_chars: int
```

#### 5.2.4 批处理策略

- 分批规则：**同时满足** ① 条数 ≤ 16，② 累计字符数 ≤ 4500（留 500 给协议头余量）。
- 顺序保持：批内顺序与原 cue 一一对应；调用结束按 index 写回 `cue.translation`。
- 失败重试：整批重试 3 次 → 仍失败则按 1/2 二分继续 → 最终无法翻译的 cue 标记 `translation = "[翻译失败]"`。

#### 5.2.5 语言检测策略

1. 优先使用 OCR 返回的 `lang`。
2. 当 cue.lang 缺失或多帧 lang 不一致 → 合并阶段取主导 lang（众数）。
3. 当主导 lang 仍为空 → 调 `LangDetect` 兜底（每个视频仅调一次，取前几条非空 cue 拼接送检）。
4. 如果检测到 `zh` 且目标也是 `zh` → 不调用翻译。

#### 5.2.6 翻译缓存

- 同样原文不再二次翻译（dict 内存缓存）。
- 短剧台词重复率不低，缓存可减少 10~30% 调用量。

#### 5.2.7 关键参数表

| 参数 | 默认 | 说明 |
|------|------|------|
| `target` | `zh` | 中文 |
| `source` | 自动 | 传 OCR 探测出的 lang，提升翻译质量 |
| `max_batch_items` | `16` | 接口硬限 |
| `max_batch_chars` | `4500` | 接口硬限 5000，预留 buffer |
| `qps_limit` | `5` | 与账户配额一致 |
| `cache` | true | 相同原文不重复翻译 |

---

### 5.3 数据模型（`pipeline/models.py`）

```python
@dataclass
class SubtitleCue:
    start: float
    end: float
    text: str              # 原文
    lang: str = ""         # 主导语种（th/en/ar/...）
    translation: str = ""  # 中文翻译

@dataclass
class OCRSample:
    t: float
    text: str
    score: float
    lang: str = ""         # 新增：语种标识
```

### 5.4 多语种文本处理适配

现有 `looks_like_dialogue()` 硬编码了中文字符判定，需改为多语种版本：

- **判定逻辑**：至少 N 个可见字符（去空白、标点后）+ 不命中 banned 列表 + 字符种类与 lang 一致（如 lang=th 但全是 latin → 丢弃）。
- **normalize_text() 按语种分支**：
  - 中文：保留原逻辑（去空白）；
  - 西文（en/de/fr/es）：保留单词间空格，去多余空格；
  - 泰文（th）：保留原始字符（泰文不用空格分词）；
  - 阿拉伯语（ar）：保留 RTL 字符，不做 lower()，不乱删标点。
- **合并阈值**：泰语/阿拉伯语 `sim_threshold` 建议降到 0.78，英语保持 0.86。

### 5.5 保留不动的模块

以下逻辑从原 `extract_subtitles.py` 迁移到对应文件，**仅搬迁不改逻辑**：

| 模块 | 源函数 | 目标文件 |
|------|--------|----------|
| softsub | `ffprobe_streams()`, `has_subtitle_stream()`, `try_extract_soft_subtitle()`, `read_srt_text()`, `parse_srt_time()` | `pipeline/softsub.py` |
| sampler | `crop_subtitle_roi()`, `preprocess_for_ocr()` | `pipeline/sampler.py` |
| merger | `merge_samples_to_cues()`, `postprocess_cues()`, `similarity()`, `normalize_text()` | `pipeline/merger.py` |
| writer | `format_srt_time()`, `format_vtt_time()`, `write_srt()`, `write_vtt()`, `write_txt()` | `pipeline/writer.py` |

---

## 6. 配置与依赖变更

### 6.1 `requirements.txt`

```
# 火山引擎 SDK
volcengine>=1.0.150

# HTTP 兜底（如不用 SDK 手动签名）
requests>=2.31.0

# 视频/图像处理（保留）
opencv-python-headless>=4.8.0
numpy>=1.24.0,<2.0.0
Pillow>=10.0.0

# 工具
tqdm>=4.66.0
rapidfuzz>=3.6.0
python-dotenv>=1.0.0
```

**移除**：`paddleocr`、`paddlepaddle`（及所有 paddle 依赖）。

### 6.2 环境变量

| 变量 | 必填 | 默认 | 说明 |
|------|------|------|------|
| `VOLC_ACCESS_KEY_ID` | 是 | - | AccessKeyId |
| `VOLC_SECRET_ACCESS_KEY` | 是 | - | SecretAccessKey |
| `VOLC_REGION` | 否 | `cn-north-1` | 服务 Region |
| `VOLC_OCR_QPS` | 否 | `5` | OCR 限速 |
| `VOLC_TRANSLATE_QPS` | 否 | `5` | 翻译限速 |

### 6.3 `.gitignore` 追加

```
.env
.volc/
failed_frames.json
ocr_frames/
```

---

## 7. 命令行接口设计

保留所有原 CLI 参数（`--out`、`--interval`、`--roi`、`--x-margin`、`--ban`、`--sim-threshold` 等），新增/修改如下：

| 参数 | 类型 | 默认 | 说明 |
|------|------|------|------|
| `--ak` | str | env | AccessKeyId，未传则读环境变量 |
| `--sk` | str | env | SecretAccessKey，未传则读环境变量 |
| `--ocr-mode` | str | `text_block` | `default` 或 `text_block` |
| `--ocr-min-prob` | float | `0.6` | 客户端二次置信度过滤 |
| `--ocr-qps` | float | `5` | OCR 限速 |
| `--source-lang` | str | `auto` | `auto`（自动检测）/ `th`/`en`/`ar`/... |
| `--translate` | flag | False | 启用翻译为中文 |
| `--target-lang` | str | `zh` | 翻译目标语言 |
| `--translate-qps` | float | `5` | 翻译限速 |
| `--bilingual` | flag | False | 同时输出双语 SRT（需 --translate） |
| `--keep-original` | flag | True | 是否保留外文版本 |
| `--max-image-side` | int | `1920` | 超过则等比缩小 |
| `--jpeg-quality` | int | `85` | 上传图片 JPEG 质量 |
| `--report` | flag | False | 输出 `<stem>_report.json` |

**废弃参数**：
- `--lang`（原 PaddleOCR 语言码）→ 替换为 `--source-lang`。
- `--min-score` 含义保留，默认值改 0.6。

**使用示例**：

```bash
# 基础：只做 OCR 不翻译
python extract_subtitles.py "./videos/*.mp4" --out ./subtitles

# OCR + 翻译 + 双语
python extract_subtitles.py "./videos/*.mp4" --out ./subtitles --translate --bilingual

# 指定源语言（提升 OCR/翻译质量）
python extract_subtitles.py "./videos/*.mp4" --out ./subtitles --translate --source-lang th

# 调整采样间隔和 ROI
python extract_subtitles.py "./videos/*.mp4" --out ./subtitles --translate --interval 0.4 --roi 0.50 0.86
```

---

## 8. 输出格式设计

每个视频生成的文件：

| 文件 | 触发条件 | 内容 |
|------|----------|------|
| `<stem>_原文字幕.srt` | 总是 | OCR 原始外文字幕 |
| `<stem>_原文字幕.vtt` | 总是 | 同上 VTT 版 |
| `<stem>_原文字幕.txt` | 总是 | 纯文本 |
| `<stem>_中文字幕.srt` | `--translate` | 翻译后中文 |
| `<stem>_中文字幕.vtt` | `--translate` | 同上 VTT |
| `<stem>_中文字幕.txt` | `--translate` | 纯文本 |
| `<stem>_双语字幕.srt` | `--translate --bilingual` | 每条 cue 两行：原文 + 中文 |
| `<stem>_report.json` | `--report` | 调用次数、耗时、估算成本 |

双语 SRT 示例：

```
1
00:00:01,200 --> 00:00:03,500
สวัสดีค่ะ
你好

2
00:00:03,800 --> 00:00:06,100
I miss you so much
我好想你
```

report.json 字段：

```json
{
  "video": "ep01.mp4",
  "duration_sec": 1320,
  "sampled_frames": 2640,
  "ocr_calls": 2640,
  "ocr_failed": 3,
  "translate_calls": 12,
  "translate_chars": 8421,
  "elapsed_sec": 480,
  "estimated_cost_cny": 1.27
}
```

---

## 9. 性能与成本估算

### 9.1 单视频用量模型（短剧 1 集 ≈ 90 秒）

| 项目 | 计算 | 数量 |
|------|------|------|
| 抽帧数 | 90s / 0.5s | 180 帧 |
| OCR 调用 | 1 帧 1 次 | **180 次** |
| 合并后 cue 数 | 经验值 | 约 60 条 |
| 翻译总字符 | 60 × 25 字符 | **1500 字符** |
| 翻译批次（≤16/批） | ceil(60/16) | **4 次** |

### 9.2 单视频费用估算（以官网最新价为准）

- OCR：按 ¥0.015/次 × 180 ≈ **¥2.70**
- 翻译：按 ¥49/百万字符 × 1500 / 1,000,000 ≈ **¥0.07**
- **合计 ≈ ¥2.8 / 集**

### 9.3 优化方向

| 优化手段 | 预期收益 | 复杂度 |
|---------|---------|--------|
| 相邻帧 SSIM > 0.95 跳过 OCR | 降低 30~60% 调用 | 中 |
| 并发线程池（20 QPS） | 180帧从 36s 降到 9s | 低 |
| 翻译缓存（相同原文不重复） | 降低 10~30% 翻译调用 | 低 |
| interval 自适应（转场用 1s） | 降低 20~40% 帧数 | 高 |

### 9.4 端到端预期耗时（单视频）

| 阶段 | 5 QPS | 20 QPS |
|------|-------|--------|
| 抽帧 + 预处理 | 30s | 30s |
| OCR | 36s | 9s |
| 合并 | <1s | <1s |
| 翻译 | 1s | 1s |
| 写出 | <1s | <1s |
| **总计** | **~70s** | **~42s** |

---

## 10. 注意事项与边界情况

### 10.1 API & 网络

1. **签名时间偏差**：本机时间与火山服务器偏差 >15 分钟会被拒签，生产机务必启用 NTP。
2. **限流（QPS 超限）**：典型 code `50429`。客户端必须实现 token bucket，否则批跑会大面积失败。
3. **超时**：OCR 接口 P95 ≈ 500ms，建议 timeout=10s；翻译接口 P95 ≈ 300ms，建议 timeout=10s。
4. **重试幂等性**：OCR/翻译都是幂等接口，可以放心重试；但每次重试都计费，建议设置上限。

### 10.2 图像

5. **图片过小**：火山要求 ≥256×256。ROI 裁剪后若任一边 <256，需用 `cv2.resize` 补到 256。
6. **图片过大**：单边 >2048 或 Base64 >8MB 会被拒。预处理后必须缩放到 max_image_side=1920，JPEG 质量控制在 85。
7. **内存中编码**：用 `cv2.imencode('.jpg', prep, [IMWRITE_JPEG_QUALITY, 85])` 返回 bytes，不再落盘写临时 PNG 文件。
8. **空白帧/转场帧**：OCR 返回空 `ocr_infos`，pipeline 视为「无字幕」，不计入 samples。

### 10.3 语种与文本

9. **多语种混排**：泰语 + 英文姓名常见。`text_block` 模式会合并为一条，`lang` 字段给出主导语言。
10. **阿拉伯语 RTL**：SRT/VTT 中阿拉伯文需加 `‫`（RLE）/`‬` 包裹保证 RTL 渲染。建议加 `--rtl-wrap` 开关默认开启。
11. **泰语无空格**：不要用 `text.replace(" ", "")`。`normalize_text()` 需按 lang 分支处理。
12. **OCR 串行帧小漂移**：同一句话连续帧识别成不同变体是常态，合并阈值泰/阿语降到 0.78。

### 10.4 翻译

13. **空字符串/纯标点**：调用前先过滤，避免浪费配额。
14. **超长单条**：单条 >4500 字符（罕见）需先按句号切分再送翻。
15. **翻译失败回退**：整批失败 → 二分 → 单条仍失败 → `translation = "[翻译失败]"`，原文保留。
16. **翻译质量校验**：可加 `--qc-min-ratio 0.3`，若译文/原文长度比异常（<0.3 或 >4），打 warning 写入 report。

### 10.5 配置与安全

17. **AK/SK 泄漏**：禁止 commit `.env`，禁止 AK/SK 写进命令历史。README 中强烈建议使用环境变量。
18. **多账户/多 Region**：留好 `region` 参数，未来切到 `ap-southeast-1` 等海外节点无需改架构。
19. **断点续跑**：检测 `<stem>_原文字幕.srt` 是否已存在且非空，存在则跳过 OCR；翻译同理。

### 10.6 兼容性

20. **保留 `--force-ocr`**：行为不变，跳过软字幕。
21. **保留软字幕路径**：软字幕也支持 `--translate`，相当于「外挂字幕 → 翻译为中文」。
22. **OpenCV seek 精度**：`cap.set(POS_MSEC)` 对部分 VBR/MP4 不精确，当出现时间戳跳跃时可回退到按帧号 step 方案。

---

## 11. 实施步骤

| 阶段 | 内容 | 验收标准 |
|------|------|----------|
| PR-1 | 重构骨架：拆分 pipeline/ 包，迁移原逻辑（仍用 PaddleOCR 跑通） | 原有测试/用例通过 |
| PR-2 | 接入 OCR：新增 `ocr_volc.py`，替换 PaddleOCR | 跑 1 个泰文短片，输出 `_原文字幕.srt` 非空且行数 >5 |
| PR-3 | 接入翻译：新增 `translator_volc.py` + `--translate` + `--bilingual` | 加 `--translate` 输出 `_中文字幕.srt`，行数与原文一致 |
| PR-4 | 完全移除 PaddleOCR：清理 requirements.txt、删除旧代码 | `pip install` 无 paddle 依赖 |
| PR-5 | 性能优化：相邻帧跳过 + 并发线程池 + 翻译缓存 + report | 单视频 90s 短剧处理时间 <45s（20 QPS） |

---

## 12. 风险登记

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| 火山接口字段命名变化 | 中 | 解析失败 | dataclass 保留 `raw` 字段；版本号锁定 |
| QPS 申请未到位 | 中 | 批跑耗时翻倍 | 客户端退避；支持分 AK 并跑 |
| 翻译质量不稳定（俚语/双关） | 中 | 字幕生硬 | 火山支持术语表（Glossary），由运营维护 |
| 网络抖动 | 低 | 偶发失败 | 重试 + failed_frames.json 二跑 |
| 成本失控 | 中 | 预算超支 | report.json + `--max-cost-cny` 软上限 |

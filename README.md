# subtitle_extractor_starter

本项目把“先查软字幕，查不到再用硬字幕 OCR”的流程做成了一个批处理脚本。

## 1. 安装系统工具

先安装 FFmpeg，并确认命令行可用：

```bash
ffmpeg -version
ffprobe -version
```

## 2. 安装 Python 依赖

建议新建虚拟环境：

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
python -m pip install -U pip
```

安装 OCR 依赖。CPU 版本可参考 PaddleOCR 官方安装方式：

```bash
python -m pip install paddlepaddle==3.2.0 -i https://www.paddlepaddle.org.cn/packages/stable/cpu/
python -m pip install "paddleocr[all]" opencv-python numpy
```

GPU 环境请按你的 CUDA 版本安装对应的 `paddlepaddle-gpu`。

## 3. 批量提取

```bash
python extract_subtitles.py "./videos/*.mp4" --out ./subtitles
```

强制按硬字幕 OCR：

```bash
python extract_subtitles.py "./videos/*.mp4" --out ./subtitles --force-ocr
```

如果字幕位置偏高或偏低，调裁剪区域：

```bash
python extract_subtitles.py "./videos/*.mp4" --out ./subtitles --roi 0.50 0.86
```

输出文件：

- `*_提取字幕.srt`
- `*_提取字幕.vtt`
- `*_字幕文本.txt`

## 4. 常用调参

- `--interval 0.5`：每 0.5 秒抽一帧。数值越小越准，越慢。
- `--roi 0.55 0.88`：裁剪画面高度 55% 到 88% 的区域，避免识别底部免责声明。
- `--ban 免责声明 版权 ShortTV`：过滤水印/免责声明关键词。
- `--sim-threshold 0.86`：相似字幕合并阈值。
- `--gap-tolerance 1.2`：OCR 漏识别时允许的空白间隔。

## 5. 是否需要 Agent

不需要。这个任务更适合确定性的 pipeline。Agent 适合做自动调参、多策略重试、人工质检编排等增强功能。

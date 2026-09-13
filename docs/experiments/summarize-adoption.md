# summarize 采用结论（M0）

记录日期：2026-09-13。本笔记是实施计划 Task 0.1 的采用门槛，不是根据 README stars 或宣传语作出的集成决定。

## 核对过的上游事实

| 项 | 记录 |
| --- | --- |
| 包 | `@steipete/summarize` |
| 发布版本 | **0.21.14**（npm / GitHub Release，2026-09-11） |
| 当时 `main` 顶端 | `5b1f563a9649526a4d435f0a9ff5c8abe62e3748`（2026-09-12，晚于 release tag） |
| 许可证 | MIT（`LICENSE`，Copyright 2026 Peter Steinberger） |
| 安装 | `npm i -g @steipete/summarize`；Homebrew `brew install summarize`；`npx -y @steipete/summarize` |
| Node | README / `package.json` `engines.node`：**>=24** |
| 可选本机工具 | `ffmpeg`、`yt-dlp`（YouTube slides）、`tesseract`（`--slides-ocr`） |
| 相关公开接口 | [`docs/commands/slides.md`](https://github.com/steipete/summarize/blob/main/docs/commands/slides.md)、[`docs/slides.md`](https://github.com/steipete/summarize/blob/main/docs/slides.md)、[`docs/install.md`](https://github.com/steipete/summarize/blob/main/docs/install.md) |

`summarize slides <source> --json` 的文档信封为 `{ ok, slides: { sourceUrl, slidesDir, slides[] } }`，slide 字段含 `index`、`timestamp`、`imagePath`、可选 `ocrText`/`ocrConfidence`。文档写明默认 `--slides-max` 为 **6**，并用 FFmpeg 场景检测抽帧。`--extract --json` 被描述为稳定自动化信封，但公开命令文档没有给出与 yt2class TranscriptDocument 同构的分段/origin schema。

yt2class 不 fork summarize。可选适配器是受控 argv 子进程：`src/yt2class/adapters/summarize_cli.py`。

## 本环境实际跑了什么

| 实验 | 状态 |
| --- | --- |
| 离线 slides/extract fixture 合同 | **已做**。`tests/contract/test_summarize_cli.py` 校验合法时间、PNG/JPEG 可读、YouTube/local 来源可区分、`ok:false` 与非零退出码不得变成成功。 |
| 与原生路径的文档化样本对照 | **已做（离线）**。同一合成时间戳 `[12.5, 40.0]` + sidecar 字幕与 `NativeIngestExpectation` 对齐；当原生期望 8 帧时，记录 summarize 默认 6 帧上限缺口。 |
| 同一本地视频 live `summarize slides/extract` | **未做**。本 Cloud Agent 环境未安装 Node 24 summarize CLI，也没有可分发的许可测试视频。 |
| 同一 YouTube 样本 live 对照 | **未做**。不能在此环境依赖 YouTube 下载稳定性或 Cookie。 |
| 下载次数、缓存清理、墙钟时间对照 | **未做**。缺少 live 运行，不能填写实测数字。 |

`@pytest.mark.live` 测试默认 skip，不进入普通 `pytest tests/contract`。

原生对照基线（yt2class 已有原型，不是猜测）：

- `yt-dlp --no-playlist` 单次下载到 run 目录，字幕 `--write-subs` / `--write-auto-subs`，命令为参数数组。
- FFmpeg 单帧抽取带明确 timestamp；场景候选由 PySceneDetect 产生，**不**按目标 PPT 页数预裁剪。
- 产物带 SHA-256 与相对路径；失败包装为 `MediaError`，不会写完整成功 manifest。

## 通过 / 不通过标准

以后若要改结论，必须同时满足：

1. 固定 summarize 版本（现在的针是 0.21.14）并在 CI 或开发者机器上可重放。
2. 本地文件 + 一个公开可下载的 YouTube 样本：全部时间合法、帧字节可读、字幕 origin 可区分（sidecar / manual / auto / asr）。
3. 失败（缺工具、下载失败、空输出）退出非零或 `ok: false`，且适配器永不返回成功结果。
4. 与原生 yt-dlp/FFmpeg 比：下载次数、缓存命中、失败清理、耗时写入本文件；不能更差到无法接受。
5. **不得**依赖默认 `--slides-max 6` 或目标页数截断未分析时间线。若必须设上限，该上限必须大于课程证据需求并写入配置，而不是 CLI 默认值。
6. JSON 字段在针定版本下稳定，或适配器明确忽略额外字段并严格校验我们需要的子集。

任一项失败则保持“不作为默认 ingestion”。

## ADOPTION DECISION

**不采用 summarize 作为默认 ingestion backend。**

可选受控子进程适配器保留，仅供以后 live 复测；`yt2class build` 继续走现有 yt-dlp / FFmpeg / PySceneDetect。

依据（已有证据，不是星标）：

1. **Live 对照缺口。** 本环境无法完成计划要求的“同一本地视频 + YouTube 样本”实跑，因此不能证明下载生命周期、缓存清理或时间戳稳定性。未证明合格就不能采用。
2. **默认 6 帧上限与设计冲突。** 公开 slides 命令默认 `--slides-max 6`。完整设计禁止按页数/过早上限丢掉未分析时间段。离线对照在原生期望 8 帧时已记录该缺口。
3. **运行时重量。** npm 包要求 Node 24+，并可选再依赖 yt-dlp/FFmpeg/tesseract。yt2class 已是 Python 3.11+ 且已有 argv 级 yt-dlp/FFmpeg 适配。为抽帧再引入整套 Node CLI 没有被 live 收益证明。
4. **合同不对齐。** 文档化 slides JSON 没有 SHA-256、timebase、半开区间字幕或 caption origin。`--extract --json` 不是已核验的 TranscriptDocument。core 的 content/prompts 入口不等于稳定抽帧 API。
5. **产品边界。** summarize 是通用摘要产品（网页、播客、LLM、浏览器扩展）。yt2class 需要课程知识、证据核验和 SlideSpec 3.0；不 fork 其 TypeScript 应用。

## 能力缺口清单（给 M1）

- 未测：YouTube 人工/自动字幕选择是否可映射到 `sidecar|manual-caption|auto-caption|asr`。
- 未测：失败时临时文件是否总被清理（文档声称失败下载会清临时文件，成功交接把清理交还给调用方）。
- 未测：`SLIDES_EXTRACT_STREAM=1` 低精度回退是否会写出不可作为证据的时间戳。
- 未测：WASM FFmpeg 回退 vs 原生 FFmpeg 的 PTS 误差。
- 已从文档确认：默认 slides 上限 6；YouTube slides 仍要 `yt-dlp`。

若 M1 live 实验满足上面的通过标准，可以把该适配器升级为**可选** ingestion backend，并继续固定版本、保留合同测试。在此之前不要把它接到 `build`。

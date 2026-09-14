# yt2class

`yt2class` 把一批 YouTube 课程链接整理成 PPTX 讲义：先下载视频和可用字幕，再按课件画面变化抓取候选帧，做感知去重，最后把经过校验的原始视频截图嵌入 PPT。它不会让模型重新生成课程图片。

> 项目状态：M0 合同、M1 证据抽取、M2 全视频理解与 M3 编辑/核验/审阅已经落地；
> PptxGenJS / SlideSpec 3.0 交付仍按实施计划的 M4 推进。

目标系统让 LLM 覆盖整段课程，而不是在目标页数范围内预先截断候选画面：

```text
视频 → 字幕/ASR＋场景/帧/OCR → 整课主题地图
     → 分段多模态理解 → KnowledgeDocument
     → 全局编辑与证据核验 → SlideSpec 3.0
     → PptxGenJS → PPTX＋来源索引＋预览/QA
```

## 快速开始

需要 Python 3.11+、`uv`、Node.js、`yt-dlp` 和 `ffmpeg`。macOS 可以先安装外部命令：

```bash
brew install yt-dlp ffmpeg node
uv sync
```

把链接逐行放入 `links.txt`（空行和 `#` 开头的行会被忽略）：

```text
https://www.youtube.com/watch?v=VXTA-hsnJEY
```

批量生成：

```bash
uv run yt2class build \
  --links links.txt \
  --output output \
  --max-slides 12 \
  --preview
```

`--force` 会重新分析并渲染；默认会复用已有的完整运行目录。使用
`--no-preview` 可以跳过逐页 PNG 预览。当前批处理顺序执行，遇到失败会停止后续链接；阶段错误包装尚不完整。

## 模型和人工复核

没有模型凭据时，工具使用离线计划：仍然保留原始画面、时间戳和证据哈希，但解释会明确标注“当前没有可用字幕”，不会假装理解画面。可通过以下任一组环境变量接入兼容视觉模型：

```bash
export YT2CLASS_MODEL_URL="https://your-endpoint/v1/chat/completions"
export YT2CLASS_MODEL_KEY="..."
export YT2CLASS_MODEL="your-vision-model"
```

也支持 `OPENAI_API_KEY`（可选 `OPENAI_BASE_URL`）以及 Anthropic 兼容配置。
当前 DeepSeek API 路由会被显式标记为文本模型并自动走离线回退，不会把图片误发给不支持视觉输入的端点；如果要得到逐图中文讲解，请配置真正支持图片输入的模型，或使用人工审核 JSON。

人工审核文件必须是严格 JSON，并且只能用候选帧的 `frame_id`（例如
`frame-0001`）；图片路径、图片 URL 等字段会被拒绝：

```json
{
  "title": "辞書形",
  "subtitle": "原始视频画面讲义",
  "slides": [
    {
      "frame_id": "frame-0001",
      "kind": "grammar",
      "title": "重点画面",
      "explanation_zh": "这里填写中文解释。",
      "takeaway": "这里填写复习要点。"
    }
  ],
  "summary": ["要点一", "要点二"],
  "quiz": [{"prompt": "请填空：____。", "answer": "参考答案"}]
}
```

单课人工复核时追加 `--selection-file reviewed.json`。候选帧和编号可在运行目录的
`frames/`、`analysis/deck.json` 中查看。

## 输出结构

```text
output/runs/<lesson-id>/
├── media/source.mp4             # 下载的原视频
├── frames/frame_*.jpg           # 场景检测后的原始截图
├── analysis/deck.json            # 绑定截图路径的可审阅规格
├── analysis/source-notes.txt     # URL、时间戳、路径和 SHA-256 来源清单
├── analysis/previews/             # --preview 生成的逐页 PNG 和布局 JSON
├── lesson.pptx
└── manifest.json                 # 选择模式、字幕状态、截图哈希和讲义计划
```

当前导出依赖 Artifact Tool（不是 PptxGenJS），还需可用的 setup helper；
代码会发现 Codex runtime 中的 helper，也可通过
`YT2CLASS_ARTIFACT_SETUP` 指定兼容的 helper。仅安装上述外部命令不足以
保证渲染可用。PptxGenJS 迁移与独立预览见下方设计计划。

PPT 固定包含封面、按课程顺序排列的原始画面页、本课总结和小测验；每页 speaker
notes 都带 `[Sources]`，原始截图以字节形式嵌入 PPTX。场景检测使用画面变化并对
相似帧去重，不是固定每 15 秒截图；长时间静态课件会按有界间隔补候选帧。

## 本地测试

```bash
uv run pytest -q
```

## M1 证据抽取能力

当前 M1 stage `yt2class.stages.extract_evidence.extract_evidence` 接受已经校验的
`SourceManifest` 和本地媒体，按 sidecar → 人工字幕 → 自动字幕 → 可选 ASR 的顺序
选择语音证据，解析 VTT/SRT 并保留原始文件 hash、语言、来源、重叠和覆盖缺口。
视觉证据使用 content/adaptive 场景 profile，在每个场景内多点采样并对长静态区间补帧；
每个 occurrence 保留 requested/actual source timestamp、图片 hash、质量指标和局部感知
cluster。OCR 是可选能力，未配置时会在 EvidenceBundle 中写出明确的 unavailable gap。

WhisperX 只通过 `yt2class.workers.whisperx_worker` 独立进程调用，普通 `uv sync` 和
CI 不会下载模型。使用真实模型前需在单独 worker 环境安装 WhisperX；普通测试使用受控
runner 和合成媒体 fixture，不把 fake JSON 当成真实模型执行。

证据文件写入运行目录的 `evidence/transcript-document.json`、
`evidence/visual-catalogue.json` 和 `evidence/evidence-bundle.json`，重复运行使用稳定
ID/hash；字幕或帧单侧失败会保留另一侧并生成可解释 gap。该 stage 不根据最终 PPT 页数
截断候选证据。

## M2 全视频理解闭环

M2 在 M1 EvidenceBundle 之上增加 CourseMap outline、分段调度、分段多模态分析、
有界补证据和知识归约。分析覆盖完整核心时间轴，**不会**按最终 PPT 页数截断。

普通测试和下面的 CLI 只使用 `FakeProvider` 与合成 fixture，用来证明合同和
编排闭环。这**不是**真实模型理解，也不把 fake JSON 当成金标准。

```bash
uv run yt2class analyze \
  --evidence output/runs/<lesson-id>/evidence/evidence-bundle.json \
  --output output/runs/<lesson-id>/analysis \
  --provider fake
```

`--provider` 目前只实现并测试了 `fake`。真实课程 + 真实视觉模型的纵向样本是
opt-in 活测，见 [tests/live/README.md](tests/live/README.md)；不要提交课程媒体、
密钥或原始模型答卷。

```bash
uv run pytest tests/unit tests/contract tests/integration -q
```

## M3 全局编辑、核验与人工审阅

M3 在 KnowledgeDocument 之上做页数约束下的 EditorialPlan、逐条 claim 核验和离线审阅包。
普通测试与下面的 CLI 只使用 `FakeProvider` 或确定性评分，用来证明合同和 M3 门槛。
这**不是**真实模型核验，也不把 fake JSON 当成金标准。

```bash
uv run yt2class plan \
  --knowledge output/runs/<lesson-id>/analysis/knowledge-document.json \
  --transcript output/runs/<lesson-id>/evidence/transcript-document.json \
  --visual output/runs/<lesson-id>/evidence/visual-catalogue.json \
  --output output/runs/<lesson-id>/editorial \
  --provider fake

uv run yt2class verify \
  --knowledge output/runs/<lesson-id>/analysis/knowledge-document.json \
  --plan output/runs/<lesson-id>/editorial/editorial-plan.json \
  --transcript output/runs/<lesson-id>/evidence/transcript-document.json \
  --visual output/runs/<lesson-id>/evidence/visual-catalogue.json \
  --output output/runs/<lesson-id>/editorial \
  --mode draft \
  --provider fake

uv run yt2class review \
  --knowledge output/runs/<lesson-id>/analysis/knowledge-document.json \
  --plan output/runs/<lesson-id>/editorial/editorial-plan.json \
  --report output/runs/<lesson-id>/editorial/verification-report.json \
  --transcript output/runs/<lesson-id>/evidence/transcript-document.json \
  --visual output/runs/<lesson-id>/evidence/visual-catalogue.json \
  --output output/runs/<lesson-id>/editorial \
  --provider fake
```

`--provider` 目前只实现并测试了 `fake`。`strict` 模式在仍有未解决的关键 claim 时拒绝写出
`verified` 标签，并退出码 2；draft / evidence-only 会在页面 notes 和 `review.html` 里留下
可见标记，不会伪装成已核验。审阅文件只允许改文案、选择 allowed frame、删页、锁定和排序；
过期 revision 或 baseline hash 会被拒绝。修改后只重跑受影响 claim 的 verifier。
SlideSpec 3.0 绑定与 PptxGenJS 渲染在 M4 实现（`stages/bind_spec.py`、`adapters/render/pptxgenjs.py`）；review 默认仍使用 stub，完整交付请调用 bind/render 阶段。构建 wheel 前在 `renderer/` 运行 `npm ci`（`scripts/prepare_renderer_bundle.sh` 或 hatch 自定义 hook 会自动执行）。


## 完整目标设计

已按现有代码更新原设计文档：

- [完整架构设计](docs/plans/2026-08-11-youtube-to-ppt-design.md)：
  产品边界、开源复用、ingestion、整段视频 LLM 理解、知识组织、截图选择、
  核验/审阅、SlideSpec 3.0、PptxGenJS、来源回链、缓存、成本和验收。
- [分阶段实施计划](docs/plans/2026-08-11-youtube-to-ppt-implementation.md)：
  从合同与复用实验到完整视频理解、PPTX 交付、恢复、hybrid 视频模型和评测。
- [SlideSpec v2 JSON Schema](docs/schemas/slide-spec.v2.schema.json) 和
  [合成示例](docs/examples/slide-spec.v2.json) 仍是原型合同，且 `slide_spec.py` 未改。
  M0 已在 `schemas/` 增加 SlideSpec 3.0 与其它关键文档的 Draft 2020-12 schema；
  它们是加法合同，**还没有**接到 `yt2class build`。

## M0 合同基线

里程碑 M0 只建立 3.0 合同与复用门槛，不重写原型流水线：

- 领域模型在 `src/yt2class/domain/`（含跨文档引用闭包和 v2→3.0 迁移报告）。
- summarize 采用结论见 [docs/experiments/summarize-adoption.md](docs/experiments/summarize-adoption.md)：默认仍走 yt-dlp/FFmpeg。
- provider / renderer 抽象在 `src/yt2class/adapters/providers/` 与 `src/yt2class/adapters/render/`；阶段编排留给 M1+。

```bash
uv run pytest tests/contract -q
uv run pytest -q
```

目前 `build` 仍是原型：仅接受 YouTube 链接，没有把 M1–M3 接到正式 PPT 导出。
v2 契约与校验已落地但尚未接入 build；它不会自动升级成目标 3.0。
不要把 v2 示例传给当前 `--selection-file`，该选项仍使用上方的 LessonPlan 格式。

开发者可独立校验 v2：

```python
from pathlib import Path
from yt2class.slide_spec import SlideSpec, validate_assets

spec = SlideSpec.model_validate_json(Path("analysis/slide-spec.v2.json").read_text())
validate_assets(spec, Path("output/runs/my-lesson"))
```

JSON Schema 校验结构，Python 模型额外检查引用、顺序与时间区间；
`validate_assets` 检查真实文件路径和 SHA-256。示例中的哈希/文件为占位，
只能演示结构，不能直接渲染。证据引用正确不代表模型解释已通过语义复核。

2026-09-13 初始检查曾有 20 通过、3 失败，原因是 Artifact Tool helper 路径过期；
现已改为运行时发现并修复。M0 之后合同测试与原型测试一并由 `uv run pytest -q` 覆盖。依赖 Codex
Artifact Tool helper 的 PPTX 渲染测试在没有 helper 的环境中会 skip，
不是合同失败。尚未运行新的端到端视频生成验收。

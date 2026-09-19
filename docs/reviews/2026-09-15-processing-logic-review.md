# yt2class 处理逻辑审阅意见（2026-09-15）

- **审阅对象**：`master@cf950f4`（PR #9 合并后）
- **审阅方式**：对 orchestration / stages / adapters 三层分别做全量静态深读；关键断言已在工作树中逐一复核（见文末"方法与置信度"）。
- **结论速览**：架构骨架（契约分层、阶段缓存、原子写、幂等、预算门禁）达到业界水准；主要问题集中在四类——**Windows 可用性阻断（P0）、运行内串行与缓存粒度（P1 性能）、LLM 成本结构（P1 成本）、若干正确性风险（P2）**。逐条均附文件:行号与建议修法。
- **Codex 复核（PR #11）**：已采纳 5 条行级意见——P1-5 降级（当前树无 wire 序列化）、P1-6 修正拷贝次数并保留 provenance 边界重哈希、P2-5 改写（不存在三次相同 JSON 重试）、删除原正确性风险 #8（verifier 幂等键含 payload digest）。详见文末修订记录。

---

## P0 · 可用性阻断（当前在 Windows 上无法正常运行）

### P0-1 顶层 `import fcntl` 使编排包在 Windows 上无法导入

- **位置**：`src/yt2class/orchestration/workspace.py:7`（裸导入，无 try/except、无平台分支），`fcntl.flock` 用于 :90、:107、:115、:132。
- **影响**：`cli.py` / `pipeline.py` / `ingest.py` / `extract_evidence.py` 均导入 workspace → 在 Windows 上 `import yt2class` 即抛 `ModuleNotFoundError`，`build-run` / `batch` / `resume` 全部不可用。已在本机（Windows 10 / Python 3.12）实测复现。
- **建议**：按平台分支实现 run 写锁——POSIX 用 `fcntl.flock`，Windows 用 `msvcrt.locking`；两者皆不可用时退化为 `os.O_CREAT | os.O_EXCL` 锁文件。

### P0-2 子进程输出按系统 locale 解码，中文 Windows 上损坏关键数据

- **位置**：`src/yt2class/adapters/process.py:124-131`（`Popen(..., text=True)` 无 `encoding=`）；同样问题：`adapters/render/pptxgenjs.py:146-153`、`render/preview.py:44`、`orchestration/tool_probe.py:16-22`（裸 `subprocess.run`）。
- **影响**：中文 locale（cp936）下按 locale 解码子进程 stdout/stderr。两条必然含非 ASCII 的数据通路：① yt-dlp 经 `--print after_move:filepath` 输出的媒体路径（B 站/YouTube 中日文标题极常见，`ytdlp.py:112-141` 依赖该输出定位文件）；② WhisperX worker 以 `ensure_ascii=False`（`workers/whisperx_worker.py:125`）打印的中文转写 JSON（`asr.py:419-427` 解析）。轻则 mojibake，重则 `UnicodeDecodeError` 崩溃。而本项目默认 `output_language: "zh-CN"`，目标部署环境正是中文 Windows。
- **建议**：`run_process` 统一加 `encoding="utf-8", errors="replace"`；WhisperX worker 子进程注入 `PYTHONUTF8=1`（或改为结果落盘文件、stdout 只传路径）。补齐其余三处裸 `subprocess.run`。

### P0-3 Windows 取消时遗留 ffmpeg 子进程，锁死输出文件

- **位置**：`adapters/process.py:39-57`（`_signal_group` 仅 POSIX 用 `os.killpg`）、`:124-131`（`start_new_session=True` 在 Windows 上被 CPython 静默忽略）、`:71-98`（`_terminate` 只杀主进程）。
- **影响**：`yt-dlp --format "bv*+ba/b"`（`ytdlp.py:62`）会派生 ffmpeg 合流子进程；取消时只杀 yt-dlp，ffmpeg 存活并持有输出 `.mp4` → 后续清理/重跑在 Windows 上报"文件被占用"。
- **建议**：Windows 路径改用 Job Object（ctypes 创建 + `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`，`CREATE_SUSPENDED` 启动后挂入），或 `_terminate` 回退 `taskkill /PID <pid> /T /F`。

### P0-4 LibreOffice PNG 预览只导出第一页，多页 deck 的 QA 永远失败

- **位置**：`adapters/render/preview.py:35-52`（`--convert-to png`）、`render/qa.py:61-66`（页数校验）、`render/pptxgenjs.py:160-171,201`（策略执行）、`orchestration/batch.py:15`（批量并发 2）。
- **影响**：LibreOffice 对 Impress 文档的 PNG 导出只写第一张幻灯片（LibreOffice 已知行为，建议以 2+ 页 deck 实测确认）。任意真实 N 页 deck → `preview.png_paths` 长度 1 → QA 报"页数 1 != spec N" → `render_complete=False`；`preview_policy="required"` 时直接报错。另外 LibreOffice 子进程**无超时**（首次建 profile 卡死常见），且无 `-env:UserInstallation`——批量并发 2 时两个实例争抢默认 profile 锁。
- **建议**：改 PPTX→PDF 一次转换，再逐页栅格化（pdftoppm / PyMuPDF）；加 ~180s 超时与每进程独立 profile 目录；`preview.py:66-68` 的 contact-sheet 分支目前只是复制第 1 页，实现或移除。

---

## P1 · 高影响（性能 / 成本 / 正确性）

### P1-1 运行内完全串行，设计文档的"同一 provider 并发 2"未实现

- **位置**：`stages/analyze_segments.py:519-572`（逐窗口 for 循环）、`stages/outline.py:258-290`（逐块串行）、`stages/verify_claims.py:539-551`（逐 claim 串行）、`orchestration/analyze.py:89-134`（阶段内严格顺序）。全仓库唯一的 `ThreadPoolExecutor` 是 `orchestration/batch.py:15,92`（`MAX_BATCH_CONCURRENCY=2`），并发的是**不同视频**的整 run，不是 run 内调用。
- **影响**：真实 provider 下，1 小时课程 ≈ 30 次分段调用 + 10–15 次 outline + 50–200 次验证调用 + 修复与精修，全部串行吃满往返延迟；墙钟时间 30–90 分钟，可压到几分钟。设计文档 §13（`docs/plans/2026-08-11-youtube-to-ppt-design.md:569`）写明"同一供应商默认并发 2"。
- **建议**：三个天然并行的循环（outline 块、窗口 dispatch、claim 校验）套有界线程池（默认 2）。前置条件：`RunBudget.charge`（`orchestration/budget.py:78-104`）与 FakeProvider 内部计数（`providers/base.py:125,241-258`）需加锁；`reduce_knowledge`/`refine_window` 因共享 `VisualCatalogue` 暂保持串行。

### P1-2 无分段粒度缓存：analysis 全有或全无，预算暂停 = 已付费调用全部作废

- **位置**：`orchestration/pipeline.py:326-424`——outline + 全部分段 + reduce 在一次 `analyze_evidence_bundle` 内完成，三个阶段产物在**全部成功后**才落盘（:412-421）；`BudgetExceeded → PipelinePaused`（:407-408）或任何中途异常（:409-410）时除 manifest 状态行外什么都没保存。部分缓存命中也不生效：快速路径（:354-390）仅在 reduce 已缓存时触发，outline 已缓存而分段未缓存时 outline 仍会重算。
- **影响**：系统中代价最大的失败模式——暂停/崩溃的 run 其 token 花费 100% 丢弃，resume 从头再算。且违反设计文档 §13（:568）自己写明的"预算不足停止新请求**并保存 incomplete**"。
- **建议**：逐窗口落盘（如 `cache/analyze_segments/<segments_key>/windows/<window_id>-<payload_digest[:16]>.json`），`analyze_segments` 跳过已存在且校验通过的窗口；`PipelinePaused` 传播前 flush 已完成窗口。窗口结果本就彼此隔离（`SegmentAnalysisOutcome`），改造是机械性的。

### P1-3 Token 估算对中文失准 ~3 倍、图片低估 4–6 倍，溢出窗口被静默丢弃

- **位置**：`stages/llm_util.py:16-17`（`CHARS_PER_TOKEN=4`、`IMAGE_TOKEN_ESTIMATE=256`，已核实）；消费方 `llm_util.py:193-218`、`orchestration/scheduler.py:99-178,315-389`、`analyze_segments.py:407-424`。
- **影响**：payload 主体是中文转写，实际约 1–1.5 字/token 而非 4——估算 7.5k 的窗口实际 20k+，到真实 provider 触发 `ContextOverflow`；当前处理是把**整个窗口标记失败**（`analyze_segments.py:491-500`），无拆分重试 → 覆盖率静默丢失。图片侧 8 帧 × 256 估算 vs ~1500 实际 = 单窗口隐藏 1 万+ token。
- **建议**：`estimate_tokens` 改为 CJK 感知（CJK 码点 ~1 token、ASCII ~4 字符/token，双桶扫描开销可忽略）；`IMAGE_TOKEN_ESTIMATE` 提到 ~1500–1700；dispatch 期遇 `ContextOverflow` 拆半重试一次而非放弃窗口。

### P1-4 验证器逐条 claim 调用 LLM（契约本身支持批量）

- **位置**：`stages/verify_claims.py:202-248`（`check_grounding`，payload `claims` 恒为单元素列表）、:229（响应校验要求 `len(rows)==1`）、:315（每 claim 一次）、:139-156 + :432-441（每页标题/每条要点/每条备注再一次）、:173-187（`page_evidence_text` 每页重建全量 evidence_index）。
- **影响**：12 页 deck × ~100 claim ≈ 100–275 次小调用，每次重读并重发 `verifier.md`、重复传输 evidence 块。`verifier.md:12-13` 与解析器本就期望 `{"verdicts": [...]}` 批量结构。
- **建议**：按共享 evidence 块分批（10–20 claim/次），校验器放宽到 N 行，批量失败的 claim 回退单条；`evidence_index` 每次校验运行构建一次并传入（:519 处已算过）；unknown-ref 已失败的 claim 短路跳过 grounding（:313-318 目前四项检查无条件全跑）；`load_prompt` 加缓存。

### P1-5 编辑器 payload 近乎翻倍；前缀缓存是 live adapter 设计债，不是当前浪费

- **位置**：`stages/edit_deck.py:612-641`（`candidates` 与 `selected` 由同一 `selected` 列表构造）；对照 `llm_util.py:56-63,121,207` 的 `sort_keys=True`。
- **当前树事实**：`model_request()` 把 `json.dumps(..., sort_keys=True)` **只用于** token 估算和 `payload_digest`；`ModelRequest` 不含 payload 字段。FakeProvider 经 `last_payload` 接收原始 dict，不经排序 JSON。新编排路径尚未接入会把 payload 序列化成 HTTP body 的 live adapter（`pipeline.py:130-133` 明确只接线 fake）。因此 `sort_keys` **当前不会**废掉 OpenAI 自动前缀缓存或 Anthropic `cache_control`，改掉它也没有文档原先声称的即时省钱效果。
- **仍成立的浪费**：editor 一次调用把近乎相同的页面表发两遍（`candidates` 比 `selected` 多 `unit_id` / `topic_id` / `kind` / `start_seconds`），输入近乎翻倍。
- **建议**：现在就去掉 editor 的重复表。前缀缓存留作 live adapter 接入约束——指令进 system role（`prompts/*.md` 仅 19–27 行、极稳定）、稳定前缀 ≥1024 token、digest 继续 `sort_keys`。等真正的 wire 序列化路径落地后再作为成本 P1 跟踪。

### P1-6 源视频一次运行被完整读盘哈希 5–7 次；全量拷贝次数因路径而异

- **位置**（本地 `copy` 链的哈希，不是二次拷贝）：`orchestration/pipeline.py:190-195`（ingest 缓存键哈希原文件）→ `stages/ingest.py:262`（原文件）→ `_copy_local` `:52-65`（`shutil.copy2` 写入 `media/{hash[:16]}{suffix}`）→ `:272`（**再哈希副本**）→ `extract_evidence.py:199-203`（`resolve_manifest_media` 复验）→ `:273-280`（`_snapshot_verified_media` 发现源与目标 realpath 相同，**跳过拷贝**，仍再哈希 dest）→ `:513`（提取后再哈希）→ `bind_spec.py:559-564`（交付再哈希）→ `render.py:43` 与 `pptxgenjs.py:116`（同一次 render **连续两次** `validate_bound_assets`）。ASR 路径另加 `asr.py:180,228` 前后双哈希。
- **拷贝事实**：本地 copy 模式只有 ingest 那一次 workspace 全量拷贝；提取阶段的 snapshot 与 ingest 目标路径相同，不会再复制一份。YouTube 路径例外：yt-dlp 按标题落盘（`ytdlp.py:57`），提取再拷到 `media/{sha256[:16]}{suffix}`，这时才有第二次全量拷贝。
- **影响**：2–4 GB 课程视频 = 每轮数分钟纯串行 IO。磁盘占用翻倍只发生在 YouTube（以及 local `reference` 首次打进 workspace）路径，不是本地 copy 的必然结果。
- **建议**：优化重复读盘，但**不要**用 `(size, mtime_ns)` 替代 provenance 边界上的 SHA-256。设计文档 §10.4（`:500-502`）要求绑定校验与嵌入前再哈希，防止校验后文件被改；同大小就地改写可以保住 size/mtime。可做的：ingest 拷贝后不必立刻重哈希（拷贝前后字节相同）；`content_sha256` 进程内可按 `(path, size, mtime, inode)` 记忆化**同一阶段内的重复读**，但 bind / render 仍必须对文件重读哈希；同一次 `render_bound_spec` 里 `render.py` 与 `pptxgenjs.py` 的两次 `validate_bound_assets` 留一次即可；YouTube/reference 的 snapshot 在已位于 workspace `media/` 时可硬链接。

### P1-7 yt-dlp：不设清晰度上限 + 字幕多次单独调用（相对 legacy 是退化）

- **位置**：`adapters/ytdlp.py:49-70`（`--format "bv*+ba/b"` 无高度上限）、:245-287（每个候选语言一次 `--skip-download --load-info-json` 调用）、:281-282（字幕写入失败直接 raise，不落入下一候选）。
- **影响**：可用 4K 时拉 4K，而下游只需 ≤1080p 帧 + 16kHz 单声道音频（带宽/磁盘/场景检测解码三重浪费）；字幕从 legacy 的单进程（`media.py:39-44` 同一调用完成）退化为 1..N 个进程。
- **建议**：主命令合并 `--write-subs --write-auto-subs --sub-langs "<lang>,<lang>-orig,en" --sub-format vtt/srt` 本地择轨；`-S "res:1080"`（或 `bv*[height<=1080]+ba/b`）封顶；`--concurrent-fragments 4`；字幕写入失败 `continue` 下一候选。

### P1-8 帧提取全分辨率无缩放（legacy 的 `scale=1280` 在重写中丢失）

- **位置**：`adapters/scenes.py:75-101`（`-q:v 2`、无 scale 滤镜）；对照 legacy `media.py:69`（`scale=1280:-2`）。
- **影响**：1080p/4K 原图直接进入 OCR（耗时随像素）、`measure_frame_quality`（纯 Python 像素遍历）、LLM 上传、最终 PPTX 体积。
- **建议**：`-vf` 加 `scale='min(iw,1280)':-2`（可配置）；`showinfo` 的 PTS 提取不受影响。另：逐样本串行 spawn ffmpeg（每次 0.3–1s 进程开销，1 小时课程 100–400 个样本）可用 4–8 线程池并行（样本彼此独立，完成后保序）。

---

## P2 · 中等收益

| # | 问题 | 位置 | 建议 |
|---|---|---|---|
| P2-1 | **四个分析配置项是死的**：`segment_seconds/overlap_seconds/max_images_per_batch/max_evidence_rounds` 定义后全仓库无任何读取点（已 grep 核实），调度器/精修用的是硬编码常量；用户调配置无效果，config 快照与实际行为不符 | `config.py:42-45`；对照 `scheduler.py:26-29`、`evidence_refinement.py:31` | 在 `analyze_evidence_bundle` 中构造并下传 `SchedulerConfig`/`RefinementBudget` |
| P2-2 | **精修循环整窗重发**：模型只问一个 30s 空洞，修复却重发全部 140s 转写 + 所有帧 + 此前累积的全部 clip（`extra_clips` 永不裁剪）→ 单退化窗口 2–3x 成本；round 计数按"请求"而非"轮次"（docstring 语义为每段最多 2 轮）；帧/剪辑余量是**全 run 全局**的，早段吃光晚段的 | `evidence_refinement.py:312-414,195-245`；`orchestration/analyze.py:116` | 只下发增量证据 + 请求区间局部转写，复用既有 `merge_native_into_unit` 合并机制；round 按 outer 迭代计数；余量改按窗口 |
| P2-3 | **OCR 三重浪费 + 生产 DAG 未接线**：按 occurrence 而非 asset 执行（同一静态幻灯片去重后仍 OCR 6 次）、严格串行、对 `reject_reason` 非空的暗帧/糊帧也 OCR；`measure_frame_quality` 纯 Python 遍历 800 万像素列表（两次全图 tobytes）；且 `AnalysisConfig` 无 ocr/asr 字段 → 帧模式永远拿不到 OCR 文本、缺字幕也永远不会触发 ASR | `extract_evidence.py:561-616`、`scenes.py:379-399,459-482`、`config.py:39-46`、`pipeline.py:303-308` | 按 asset_id OCR 一次再扇出到 occurrence；跳过已拒绝帧；`ImageStat`/numpy 提质；给 `AnalysisConfig` 加 ocr/asr 开关（openrouter 分支已在部分接线） |
| P2-4 | **`reduce_knowledge` 的 O(n²)**：`normalize_concept`/`concept_key`（纯函数）在嵌套扫描内被重算数百万次（regex+分词）；`retain_conflicts` 对全量 unit 做 O(units²×claims²)，未按时间分桶 | `reduce_knowledge.py:81-90,154-174,184-191,238-251,319-337` | `normalize_concept` 加 `@lru_cache`（一行，预计本阶段 10–100x）；claim 键预计算入 dict；冲突扫描按时间重叠分桶 |
| P2-5 | **`InvalidJsonResponse` 是死代码；坏 JSON 不会被 `call_with_retry` 连打 3 次**。全仓库无 raise 点。结构化校验在 `provider.complete()` **返回之后**抛 `SegmentContractError`，stage 恰好做 **一次** repair（`analyze_segments.py:454-490`）。非 fake 虽挂 `RetryPolicy(max_attempts=3)`，但只重试 `RetryableError`；现有 provider 抛的是 `ProviderError` 子类，连 timeout 都不会走这 3 次。原先"3 次相同 payload + 第 4 次修复"不成立。 | `retry.py:26-27`、`provider_gate.py:53-55`、`analyze_segments.py:454-490` | 删除或改写 `InvalidJsonResponse`，避免后人按"可重试 JSON"接线；repair 仍可改为只发失败输出+错误+evidence id。live adapter 接入时要把 HTTP 429/5xx 映射到 `RetryableError`，**不要**把 JSON 解析失败放进去 |
| P2-6 | **场景检测解码全片、对取消盲、native-video 模式也跑全套视觉目录**；docstring 承诺检测失败回退"单一静态场景"，实际异常直接失败整个视觉模态 | `scenes.py:225-320`、`extract_evidence.py:498-511`、`pipeline.py:754` | 传 `frame_skip`/downscale；检测循环内检查 `cancel_event`；异常时回退均匀 30s 采样并标 degraded；`mode == "native-video"` 时跳过/懒加载视觉目录 |
| P2-7 | **ASR 每次调用重新加载模型**（解释器启动 + torch import + 模型/对齐模型加载，CPU 上数十秒），批量模式每个视频重付 | `workers/whisperx_worker.py:71-84`、`asr.py:151-162` | 保持 JSON 契约，改常驻 worker（stdin 逐行请求/逐行响应），模型每会话加载一次 |
| P2-8 | **ffmpeg 剪辑 `-ss {start} -to {end}` 均为输入选项**，相对语义跨 ffmpeg 版本漂移 → native 模式剪辑终点可能偏移 | `ffmpeg.py:285-296` | 改 `-ss {start} -i input -t {end-start}`；对固定 ffmpeg 版本加回归测试 |
| P2-9 | **字幕重叠扫描 O(n²)**：每小时滚动字幕数千 cue → 数百万次 Python 比较；文件被读两次（哈希 `read_bytes` + 解析 `read_text`） | `subtitles.py:339-344,322-325` | 按 start 排序后扫描线（cue 只可能与其序邻重叠）；从已读 bytes 解析 |
| P2-10 | **预算超支丢弃已完成的响应**：先调 LLM 后扣费，扣费抛 `BudgetExceeded` 时已花钱拿到的结果被丢弃；且只能事后检测超支（`would_exceed` 已存在但仅被 `charge` 间接触达） | `provider_gate.py:37-51` | 超支时仍返回结果并置 `paused` 只挡后续 dispatch；dispatch 前用请求自带估算做 `would_exceed` 预检 |
| P2-11 | **`atomic_write_bytes` 写后整文件重读算 SHA-256，而返回值无人消费** | `orchestration/cache.py:126-137` | 直接对在手 bytes `hashlib.sha256` |
| P2-12 | **缓存命中双重解析；evidence bundle 序列化并落盘两次**（工作副本在纯命中时也重写） | `pipeline.py:223-224,288-289,475-476,527-528,620-621`、`:291-294,311-315` | 校验器返回已解析模型；单次序列化写双目标；命中时跳过未变化的工作副本 |
| P2-13 | **payload 每 (窗口×批次) 被完整重建并 JSON 序列化 3–5 次**；`load_prompt` 每次读盘；`accepted_occurrences` 每次重扫全目录；`SegmentAnalysisOutcome.payload` 全程驻留内存但从未被消费（仅 hybrid 路径用其 digest） | `llm_util.py:51-53,90-96,56-63,207-217`、`analyze_segments.py:230-250,512`、`scheduler.py:123-178`、`hybrid_analysis.py:363` | payload 构建一次传递；`lru_cache` 包 `load_prompt`；序列化结果复用于估算与 digest；payload 落盘后即刻丢弃（只留 digest） |
| P2-14 | **manifest/文档 O(n²) 模型拷贝与线性重扫**：`set_window_status` 每次深拷贝整个 `SegmentManifest`（每窗口 2 次）；`iter_claims()` 每 claim 重扫；`_replace_claim_text` 每修复整体拷贝文档 | `scheduler.py:482-497`、`analyze_segments.py:547-563`、`verify_claims.py:539-541,342-346,369-377`、`domain/knowledge.py:113-114` | 本地累积、结束构建一次；claim/unit 索引化 |
| P2-15 | **bind_spec 重复索引重建**：verdict 字典、transcript/OCR 字典、asset 字典在逐 claim 循环内反复重建；`_mime_for_path` 整文件 `read_bytes` 只为看 3–8 个魔数 | `bind_spec.py:140-151,228,264,290,315-319,584,90-92` | 索引提升到 `bind_editorial_plan` 顶部；魔数用 `read(8)` |
| P2-16 | **Node 渲染器是唯一不走 `run_process` 的适配器**：裸 `subprocess.run` 无超时、无取消——Node 卡死则交付阶段永久挂起 | `render/pptxgenjs.py:146-153` | 接入 `run_process`（timeout ≈300s + cancel_event） |
| P2-17 | **工具探测每次运行 2 个子进程**（`yt-dlp --version` / `ffmpeg -version`，还喂进缓存键）；`qa.py` 用 `list(getdata())` 找极值；`process.py` 对小时级下载用 50ms 轮询 | `tool_probe.py:31-43`、`qa.py:29-35`、`process.py:106` | 按 (二进制路径, mtime) 进程内缓存；`Image.getextrema()`；长命令轮询放宽到 250–500ms |

---

## 正确性风险（顺带发现，建议一并跟踪）

1. `_complete_verifier` 裸 `except Exception: return None`——连 `RequestCancelled` 也吞掉：取消后逐 claim 循环继续跑完剩余全部调用，被取消的 run 仍可能产出不完整交付物（`verify_claims.py:488-491`）。
2. outline 阶段坏 JSON 无修复重试（分段/编辑均有）：一个块的 topic 全部校验失败时该块覆盖从 CourseMap **永久丢失**（`outline.py:274-290`）；且 `evidence_in_range` 的 `ocr_by_parent` 字典每帧只保留**最后一个** OCR region id（tesseract 每词/行一个 region）→ outline 的 `allowed_evidence_ids` 缺失大部分 region，两阶段口径不一致，引用被拒为"越界"（`llm_util.py:113-117` vs `analyze_segments.py:139-142`）。
3. `text_has_negation` 的英文标记未做填充对齐检查（`"no "` 可命中 "notice"/"economy" 词内）→ 假阴性触发不必要的修复调用（`llm_util.py:176-178`、`analyze_segments.py:203-205` 同模式）。
4. `cache_policy.py:60-64` 裸 `except Exception`：任何校验错误（含 Windows 杀软瞬时锁文件）即作废该阶段**及全部下游**缓存树——应收窄为 `(OSError, CacheCorrupt)` 或先重试一次。
5. ingest 缓存仅按 URL 键控：媒体被删后重新下载到不同字节时，缓存命中返回旧 sha → 在 `resolve_manifest_media` 硬死而非重新摄取（`pipeline.py:196-197`、`ingest.py:199-203`）。
6. editorial 磁盘优先策略可能悬空交付：有 plan 文件即跳过 `plan_deck` 且跳过写产物；若 `verification-report.json` 缺失，`run_delivery_stages` 以裸 `FileNotFoundError` 硬崩而非类型化恢复路径（`pipeline.py:486-488,569-576,593-595`）。
7. `batch` fail-fast 会中断**进行中**的已计费调用（`batch.py:102-105`）——建议让在途 future 跑完、只阻止新任务。
8. Windows 长路径：`ytdlp.py:57` 输出模板标题长度无上限，深层 run 目录 + 长 CJK 标题可能超 MAX_PATH 260——加 `--windows-filenames --trim-filenames 120`。
9. 生产路径的精修默认死路：pipeline 不传 extractor → 每次精修 pull 必然失败、白耗一个 round（`pipeline.py:395-406`）——接线 `extract_frame_with_timestamp` 或 `extractor is None` 时快速跳过。

（原第 8 条"验证请求 id 截断到 60 字符会冲突"已删除：`verify_claims.py:486` 实际写成 `f"{request_id[:60]}:{request.payload_digest[:32]}"`，payload 含完整 claim id 与文本，前缀碰撞不会共用幂等槽。）

---

## 一行级 Quick Wins（性价比最高，建议先做）

1. `@lru_cache` → `normalize_concept`（`reduce_knowledge.py`）——本阶段 CPU 提速 10–100x。
2. `@lru_cache` → `load_prompt`（`llm_util.py:51-53`）。
3. payload 去除三处纯重复：`frames[].ocr_text` vs `ocr` 数组（同帧 OCR 文本发两遍）、`evidence_ids` vs `allowed_evidence_ids`（逐字重复）、editor 的 `candidates` vs `selected`（近整表重复）。
4. `atomic_write_bytes` 直接对在手 bytes 求 SHA-256。
5. CJK 感知 token 估算（一个双桶扫描函数）+ `IMAGE_TOKEN_ESTIMATE` 提额。

---

## 已达到业界水准、无需改动的部分

- 阶段级内容寻址缓存 + 下游定向失效 + 命中重校验（`cache.py`、`cache_policy.py`）；全链路原子写（temp→fsync→rename）。
- 请求幂等指纹（`providers/base.py:127-204`）；重试策略（`Retry-After` 遵从、指数退避+抖动、401/403 不可重试分类）。
- 半开区间纪律贯穿转写/帧/topic；场景范围归一为无遗漏时间线；`scheduler` 断言核心窗口完整铺满时长。
- 适配层：全程无 shell argv（无 Windows 引号问题）；seek 式抽帧（`-ss` 前置 + `showinfo` PTS 校验，不整片解码）；16kHz mono s16le 音频链路正确；媒体哈希 1MB 分块流式；WhisperX 懒导入保 CI 轻量；渲染器依赖构建期打进 wheel、图片按路径传递。
- 字幕解析对 BOM/CRLF/滚动字幕去重健壮；`cancel_event` 贯穿所有适配器（场景检测是唯一盲区，见 P2-6）。

---

## 业界实践对照

| 业界做法 | 本项目现状 | 差距 |
|---|---|---|
| 混合帧采样：均匀基线 + 场景检测 + 嵌入/感知哈希去重（60min 视频 ~7200 帧压到 50–200 关键帧） | 场景检测 + 帧质量采样 + sha256 字节级去重 + average-hash 已有雏形 | 方向正确；可升级为按感知哈希聚类（`_cluster_id` 目前只与各族代表比较，近似重复链会分裂） |
| 层级化摘要与中间层缓存（segment → 场景 → 全片） | 两层（segment → reduce），阶段级缓存 | 可增加场景级中间产物，重跑时复用中间层 |
| Prompt 前缀缓存（最高 ~90% 折扣，静态前缀 ≥1024 token） | 当前无 live HTTP 序列化路径；`sort_keys` 只服务 digest/估算（见修订后的 P1-5） | live adapter 接入时把指令放进 system role，不要按字母序打乱稳定前缀 |
| Batch API（离线任务 ~5 折） | 未使用 | 本项目纯离线，天然适配，provider 接入后即可启用 |
| 结构化输出：provider 原生 JSON schema 约束解码 | 自研 `structured_coerce`（openrouter 分支）+ 错误回注修复 | 分支方案是业界上一代的加强版；接入 provider 后可评估原生 structured output |
| 模型分级路由（便宜模型处理易片段，省 60%+） | M6 hybrid 已做"模式级"分级（frames vs native clip） | 可下沉到模型级：文本归并用便宜模型，图表密集帧用旗舰 |
| 运行内并发 fan-out + 信号量 | 完全串行（P1-1） | 设计文档已有指标（并发 2），仅未实现 |

---

## 建议动手顺序

1. **P0 全部**（P0-1/P0-2 不修，Windows 上既跑不起来、跑起来结果也是坏的；P0-4 使交付 QA 名存实亡）。
2. **Quick Wins 一批**（半天量级，无风险）。
3. **P1-2 分段缓存 + P1-1 运行内并发**——这两项决定真实 provider 接入后的成本曲线与墙钟时间。
4. **P1-3 / P1-4 + editor 去重（P1-5 仍成立部分）**——真实 LLM 上线前的成本件：估算失真、验证批量、编辑调用去掉重复表。前缀缓存等 live adapter 接线时做，不要当成当前 P1。
5. 其余 P2 按表格逐项消化，正确性风险清单并行跟踪。P2-5 不再是"停掉三次 JSON 重试"，只是清掉死类型、收紧未来 HTTP 映射。

---

## 方法与置信度

- 三路并行静态深读（orchestration / stages / adapters），逐条给出行号；行号基于 `master@cf950f4` 工作树。
- 以下关键断言已在工作树二次核实：`workspace.py:7` 裸 `import fcntl`（并实测 Windows 导入失败）；`config.py:42-45` 四配置项无读取点（全 src grep）；`llm_util.py:16-17,121,207` 常量与 `sort_keys` 双处序列化；`process.py:124-131` `Popen(text=True)` 无 `encoding=`。
- P0-4 的 LibreOffice"PNG 仅导出首页"为上游已知行为描述，建议以 2+ 页 deck 实测确认后定性。
- 未做运行时压测：各量化收益（IO 读写量、调用次数倍数）为静态推算，量级可信，精确倍数以实施时基准测试为准。
- PR #11 Codex 行级审阅后二次核实并改写：`ModelRequest` 无 payload 字段；`InvalidJsonResponse` 全仓库无 raise；`_snapshot_verified_media` 在 dest==source 时不拷贝；`_complete_verifier` 的 request_id 含 `payload_digest[:32]`；设计文档 §10.4 要求 bind/render 重哈希。

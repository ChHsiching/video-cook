## Problem Statement

一次 45 分钟讲座的全流程 run 暴露七类问题，全部指向同一根源：确定性工具散落在三个 skill 仓库的 scripts/ 里、由 cook 壳出调用，导致（1）入口分裂难维护；（2）装机的 skill 副本与 cook 版本错位（cook 已要对 installed skill 做 --keep-subs 探针防呆）；（3）关键约束（实测语速、字幕对齐）没有机械闸门，靠 agent 自觉，本期跳过测语速直接造成 82 句重写重合成约 6 小时返工。

## Solution

把所有确定性脚本物理合并进 cook CLI 仓库，命令面统一为 cook 子命令；在合并的同时补上本期暴露的六个能力缺口：测语速、长度闸门强制实测率、装配自动字幕对齐、双侧并组工具、下载选流最高码率、必切路径的词边界。skill 仓库退化为纯文档（SKILL.md + REFERENCE.md），不再承载可执行逻辑。

## User Stories

1. As a 视频流水线 agent, I want `cook dub measure-rate` 接收用户选中的试听音频与对应文本，输出实测语速（字/秒），so that 我不会再拿假设值写全片译稿。
2. As a 视频流水线 agent, I want 长度闸门在没有 `--rate` 实测值时拒绝运行，so that 薄稿不可能静默通过检查（本期事故的直接根因）。
3. As a 视频流水线 agent, I want 长度闸门支持 `--self-measure` 从已合成 take 现算语速，so that 中途接管的 run 也能补测。
4. As a 观众, I want 长句字幕跟着配音的实际朗读进度出现，so that 我不会看到字幕比声音慢十几秒（本期最重 17.7 秒）。
5. As a 视频流水线 agent, I want `cook dub assemble` 默认自动做字幕对齐（`--no-retime` 可退回旧行为并打警告），so that 默认行为就是正确的，不依赖我记住额外步骤。
6. As a 视频流水线 agent, I want 对齐用 faster-whisper 词时间戳跑每句干净人声轨（_audio_pad），so that 混音 BGM、whisperX CTC 字对齐、混音 ASR 三种已实证的错法不会复发。
7. As a 视频流水线 agent, I want `cook dub merge` 作为唯一合法并组入口，中英两侧同步修改并断言配对不变式，so that 单侧手工编辑造成的 8 句错位事故（三道审查全漏）不可能再发生。
8. As a 观众, I want 下载自动选到目标分辨率下码率最高的视频流（同档 vp9 优先于 av01），so that 我看到的画质就是源站最高（av01 假最高已四次实锤）。
9. As a 观众, I want 字幕切分永远不把一个完整词劈在两条里（如「技」「能」），so that 我不会看到残词字幕。
10. As a 视频流水线 agent, I want `cook dub synth --workers N --stagger S` 支持多进程错峰合成，so that 361 句从 21 小时缩到 9 小时且不因同时加载打爆硬盘。
11. As a cook 维护者, I want 所有阶段脚本住在 video-cook 一个仓库里，so that 改一处不用跨三个仓同步、也不会出现装机副本与 cook 版本错位的探针补丁。
12. As a cook 维护者, I want 阶段脚本通过 cook 安装包内的路径解析（而非已安装 skill 的路径），so that `pip install -U` 一次升级全部生效。
13. As a 视频流水线 agent, I want 对齐、并组、闸门都是纯函数可单测，so that 回归有 pytest 兜底。
14. As a 用户, I want 本期已验收成片的字幕时刻作为回归基准，so that 新对齐实现与人工验收结果一处不差才算过。

## Implementation Decisions

- **物理合并**：video-dubbing-skill 的 full_dub.py（608 行，synth/assemble 阶段）与 video-subtitle-skill 的 subtitles.py（672 行，shorten/biliteral/ass）及其余确定性脚本（length_gate、build_merge、scan_reference、loudness_match、make_zh_dub_srt、make_full_srt、make_zh、transcribe、extract_reference）全部迁入 video-cook 仓库（新 stages/ 模块布局）；cook.py 改为解析包内路径；重依赖阶段仍以 `--python` 指定的 venv 子进程执行，但执行的是包内文件。
- **新增 `cook dub measure-rate`**：输入试听 wav+文本（或 segments 目录自动配对），VAD 测语音跨度，输出 chars÷voiced-seconds；打印并可选写入 STATE 类 JSON。
- **闸门强制率**：长度闸门（迁入后为 `cook dub gate`）`--rate` 必填，缺失即非零退出；`--self-measure` 时从 dubbed/_full/_segments 现算。
- **装配自动对齐**：assemble 阶段在 biliteral 之后、ASS 生成之前插入 retime 步骤——faster-whisper（试听预筛同款 small 模型）对每个 _audio_pad 干净人声轨取词时间戳，SequenceMatcher 把译文字符映射到时间锚，两遍式改写双语 SRT 起点（锚点先行、驻留条继承前条、末尾单调钳制；显示条 end=下条起点-0.02；首条提前 0.05s）；EN 网格吸附造成的亚秒重叠在 ASS 生成前预修剪。`--no-retime` 保留旧行为。faster-whisper 缺失时降级为旧行为并打显式警告。
- **`cook dub merge`**：输入行号组，en.full.srt 与 translations_dub.txt 双侧同步并组（时间=首句起点+末句终点，文本拼接）；不变式断言：两侧行数相等、并组集合一致、每并组首词标记匹配；失败即拒写。
- **下载选流**：同分辨率内选 (codec 优先级 vp9>h264>av01, 码率) 排序取最优；落盘后 ffprobe 核对 codec 并写进 source.json。
- **词边界必切路径**：split_zh 的标点阶梯走尽仍超限的子句，用 jieba 求最近词边界再切；1.15x 豁免保持不变。
- **synth 多进程**：`--workers N`（默认 1）按 cue 序号带切分（降序 worker+升序主管），`--stagger S` 秒错峰启动；skip-if-exists 语义不变。
- **ADR 两篇**：「确定性脚本集中于 cook CLI」「retime 为装配默认行为」。
- **skill 仓库退化**：scripts/ 移除（或留 deprecated 指针），SKILL.md 的调用说明改为 cook 命令。

## Testing Decisions

- 单元层（主）：沿用 test_cook.py 的假 SRT+临时目录、零网络零 ffmpeg 风格，新增阶段脚本测试文件同风格；覆盖选流决策函数、--rate 强制、jieba 边界切分、merge 不变式、retime 时间计算（伪造词时间戳）。
- 真数据回归层（验收）：用本期 leerob run 的 361 个 _audio_pad 与双语 SRT 对拍——新 retime 输出必须复现 r12 成片已人工验收的字幕起点（容差 50ms）。
- 新料冒烟层：testdata/ 两段测试片（90s+75s）跑 download 选流、measure-rate 等可独立验证命令。
- 只测外部行为（命令输出与文件产物），不测内部实现细节。

## Out of Scope

- skill 仓库的文档重写（走独立的 writing-for-agents 双轴审核轨，另批执行）
- cook 的发版与装机（用户侧 pip/npx 自理）
- 语义级 zh/en 配对审查（subagent 审查轨职责，不入机械闸）
- WhisperX 转写引擎替换

## Further Notes

- 背景判例：av01 假最高 4 次；语速假设 5.0 vs 实测 6.93 造成 78.6% 覆盖率；proportional-split 字幕漂移最重 17.7s；zh/en 单侧并组造成 8 句错位 4-14s；IO 错峰为用户明示约束。
- 测试素材：视频工作目录 leerob/stanford-cs146s-always-on-agents（361 take+已验收字幕）与 video-cook/testdata/ 两段切片。

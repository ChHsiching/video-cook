# Spec: nate-herk run 回流——漏配检测 / 缓存不可损毁 / 契约对齐

## Problem Statement

nate-herk run（《Matt Pocock: The Most Valuable Skill to Learn in the Age of AI》双版本管线）暴露四类流程缺陷：

1. whisperX 在片头音乐下漏收的 19 秒语音（蒙太奇高光音条）以"静音"形态连过三轮中配交付，每次都按症状修（填充→垫原声→垫伴奏），直到第三次才做根本对账（ASR 覆盖审计）。已发布的双语字幕版也带着同一缺口（该段英文无字幕）。
2. 一次实验性的 463-cue 装配在报错夭折前已经跑完 fit pass，用错位窗口把 144 条合成 take 压坏并就地覆写缓存（`tmp.replace(wav)`）；下一次装配静默消费坏缓存出片。合成缓存是 11 小时的产物，被一次 3 秒的实验损毁。
3. 参考音更换的两次决策各赔一整轮打回：呈选未告知"窗口情绪会克隆到该说话人全部台词"（笑场事件）；换参考后速率未重校准（长句欠填）。
4. video-dubbing SKILL.md 引用 `cook dub make-srt / merge-short / loudness-match` 子命令，CLI 不存在（agent 被迫 stages 直跑）；Demucs 把片头音效误进人声轨导致伴奏床窗口性消失的失效模式无文档、修复配方无沉淀；worker 内存无启发式（3×7.7GB 挤爆 31GB 机器，用户两次干预）；全量装配会重新生成字幕冲掉手工修复的补丁仪式未文档化。

## Solution

- 漏配检测前移为固定闸：cook 新增 `dub audit-coverage` 命令（ASR 扫源音轨，语音段对账台本，JSON 缺口报告）；video-subtitle 的 Gate A 与 video-dubbing 翻译前各接一个指向它的对账步骤。
- 装配在物理上不再可能写合成缓存：fit pass 产物写每次装配重建的旁位目录，摆位与字幕窗经 fitted-优先解析器取 take。
- 三个 skill 仓文档补齐：呈选后果声明协议、参考更换点位的速率重校准强制、床混失效模式+加法混入配方、worker 内存启发式、装配后补丁重施提醒。
- CLI 补齐文档承诺的三个子命令；shorten 无句读长句逗号优先；bar-px 警告按实测帧高。

## User Stories

1. As a 配音管线 agent, I want 一条对账命令用 ASR 扫源音轨并 diff 台本覆盖, so that 音乐掩蔽下被转写器整段丢掉的语音在翻译前就被抓住。
2. As a 字幕管线 agent, I want Gate A 包含同一覆盖对账步骤, so that 双语版不再带着无字幕的语音段发布。
3. As a 管线 agent, I want fit pass 把压缩产物写进每次装配重建的旁位目录, so that 任何装配——夭折的、失败的、对着错位 cue 文件跑的——都伤不到合成缓存。
4. As a 管线 agent, I want 摆位与字幕窗通过 fitted-优先解析器取 take, so that 旁位改动对下游不可见。
5. As a 配音 agent, I want 在任意阶段能廉价重跑覆盖审计, so that 投诉驱动的诊断第一步有现成工具。
6. As a 配音 agent, I want `cook dub make-srt / merge-short / loudness-match` 真实存在, so that SKILL 文档的调用契约成立。
7. As a 配音 agent, I want shorten 在无句读长 cue 上优先逗号边界, so that "API keys"类多词术语不再被词级切分劈开。
8. As a 配音 agent, I want bar-px 几何警告按实测帧高计算期望值, so that 4K 源不再出假 mismatch 警告。
9. As a skill 读者, I want 参考音呈选步骤明说情绪克隆后果, so that 候选是在知情下选出的。
10. As a skill 读者, I want 速率重校准强制点落在"换参考"的动作点位上, so that 换参考不重测成为可见违规。
11. As a skill 读者, I want 床混的 Demucs 窗口误路由失效模式与加法混入修复配方入文档, so that 静音投诉的第一响应正确。
12. As a cooking 路由 skill 读者, I want "静音/空白投诉→三轨覆盖对账优先"的路由指针, so that 诊断顺序第一次就对。
13. As a skill 读者, I want worker 内存启发式（每 IndexTTS2 进程约 8GB）, so that worker 数不再靠猜。
14. As a skill 读者, I want "全量装配会重新生成字幕并冲掉手工修复→需重施补丁"入文档, so that 补丁仪式不用每期重新发现。

## Implementation Decisions

- **fit 旁位**：`_full/_segments_fitted/`，装配开始时清空重建；fit 产物写此目录；`take_path(idx)` 解析器 fitted-优先，摆位与字幕窗计算共用；模块 docstring 记录事故类别与设计动机。
- **audit-coverage**：新 dub 子命令；faster-whisper 扫源（raw 音轨与已分离 vocals 各一遍），语音段与 dub 台本 cue 覆盖 diff，输出 JSON 缺口清单（起止/文本/是否被既有 cue 覆盖）；ASR 依赖走模块导入替身缝（retime 的 `_import_retime_module` 模式），保证可测。
- **CLI 补齐**：三个薄子命令转发 stages 包内函数，与既有 stage 的包内调用方式一致。
- **shorten**：cue 超长且无句末标点时，先在长度上限内找最后一个逗号切分，找不到才落词级。
- **bar-px**：期望 PlayResY 比对按实测探测帧高等比缩放。
- **skills 文字**：dubbing 呈选协议/重校准触发/床混配方/内存启发式/补丁重施+对账指针；subtitle Gate A 对账步骤；cooking 路由指针。全部按 writing-for-agents 编写，双轴审（写作纪律轴+行为变更轴）通过后推送。
- **交付形态**：四仓各开分支，commit 后推 GitHub 开 PR（与 2026-09-28 reflux 先例一致）。

## Testing Decisions

- 只测外部行为，不测实现细节。cook 侧沿用两条既有缝：stage 函数级（伪 subprocess + 注入模块替身，TestAssembleAutoRetime 同款先例）与 CLI parser 接线测试；shorten 的切分是纯函数（tests/test_validation.py 先例）。audit-coverage 的 ASR 依赖用可注入替身（retime 先例），对罐头段列表断言缺口检出与既有 cue 覆盖判定。
- fit 旁位的行为断言：超窗 take 装配后摆位时长正确、缓存文件字节与 mtime 不变、旁位目录存在产物；再次装配从原始缓存重建旁位。
- skill 仓无文档测试，双轴审即闸。

## Out of Scope

- take 缓存内容哈希键（第二次序号错位事故再启动）
- 已发布双语字幕版的蒙太奇补丁（用户明示不补）
- length-gate 结构性残留的"已接受"标记
- assemble 内建的窗口级床混（配方入文档即可）

## Further Notes

- 事故正本：video-subtitle/nate-herk/matt-pocock-most-valuable-skill/STATE.md（v3/v7 段）
- 记忆判例：nate-herk-dub-v4-v7-saga / nate-herk-run-completed / reference-window-prosody-bleed-laughter
- 用户决策（2026-10-23）：范围 P0+P1+便宜 P2；双语版不动；fit 旁位（非哈希键）；对账闸两处；cook 补子命令；分支+PR

# 02: `cook dub audit-coverage`——ASR 对账扫漏

**What to build:** 新子命令对源音轨（raw 与已分离 vocals）跑 faster-whisper，检出语音段与 dub 台本 cue 做 覆盖 diff，输出 JSON 缺口清单（起止/文本/是否被既有 cue 覆盖）。音乐掩蔽下被 whisperX 整段丢掉的语音（如片头蒙太奇）在翻译前即可被抓出。ASR 依赖走模块导入替身缝（retime 先例）。

**Blocked by:** None (can start immediately).

**Status:** done

- [x] 罐头段列表注入替身：未被 cue 覆盖的语音段报为缺口，已覆盖的不报
- [x] 缺口报告含起止时间、文本、覆盖判定
- [x] CLI parser 接线测试
- [x] vocals 轨缺失时降级 raw-only 并在报告标注

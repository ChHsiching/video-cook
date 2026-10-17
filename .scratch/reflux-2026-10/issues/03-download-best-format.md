# 03: 下载选流最高码率

**What to build:** 同分辨率内按 (codec 优先级 vp9>h264>av01, 码率) 选最优视频流+最高码率音频；下载后 ffprobe 核对落盘 codec/分辨率并写进 source.json 的 cargo；testdata 真实 URL 冒烟。

**Blocked by:** None (can start immediately)

**Status:** ready-for-agent

- [ ] 同一视频下载落盘 format_id=期望的 vp9 流（fixture 单测+真实冒烟）
- [ ] source.json 记录实际落盘 codec 供下游核对

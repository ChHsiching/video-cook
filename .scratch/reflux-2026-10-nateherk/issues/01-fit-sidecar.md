# 01: fit 旁位——装配永不写合成缓存

**What to build:** 装配的 fit pass 把 atempo 压缩产物写进每次装配重建的旁位目录 `_segments_fitted/`，摆位与字幕窗计算经 fitted-优先解析器取 take；合成缓存 `_segments/` 对装配变为只读。夭折/失败/错位窗口的装配从此物理上无法损毁缓存。

**Blocked by:** None (can start immediately).

**Status:** ready-for-agent

- [x] 超窗 take 装配后：摆位时长正确、`_segments/` 原文件字节不变、旁位目录有产物
- [x] 再次装配从原始缓存重建旁位（陈旧 fitted 不泄漏）
- [x] 字幕窗计算跟随实际摆位音频（fitted 优先）
- [x] 既有 assemble 测试全绿

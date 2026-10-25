# 05: bar-px 警告按实测帧高

**What to build:** burn 的 bar-px 几何期望值按实测探测帧高等比缩放，非 1080p 源（如 4K）不再出假 mismatch 警告。

**Blocked by:** None (can start immediately).

**Status:** ready-for-agent

- [ ] 非 1080p 源在几何正确时零警告；几何真错时仍警告

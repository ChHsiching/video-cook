# 04: shorten 无句读长句逗号优先

**What to build:** cue 超长且句内无句末标点时，切分先在长度上限内找最后一个逗号，找不到才落词级。"API keys"类多词术语不再被词级边界劈开（两管线复发判例）。

**Blocked by:** None (can start immediately).

**Status:** done

- [x] 无句读超长 cue 在逗号处切分；无逗号时行为不变
- [x] 纯函数单测（tests/test_validation.py 先例）

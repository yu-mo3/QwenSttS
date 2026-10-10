# 已知问题

本文件记录**审查发现、但决定暂不修复**的问题。

存在这个文件本身就是一条规则：**决定不修的问题不能默默忽略** ——
写下来，说清楚为什么不修、什么条件下需要重新考虑。

> 流程见 [AGENTS.md](AGENTS.md#一审查流程最重要的一条)

---

## 待处理

### ~~KI-001 · 逐句串行合成，播放期间网络空闲~~ ✅ M3 已解决

| 项 | 内容 |
|---|---|
| **来源** | M2 独立审查 · Y5 |
| **级别** | 🟡 应修 |
| **位置** | `app/orchestrator/pipeline.py` · `speak()` |
| **问题** | 现在的顺序是「合成第 i 句 → 播放第 i 句 → 合成第 i+1 句」。播放期间网络完全空闲，N 句回复 = N 次 TTS 往返严格串行。审查实测：单次往返约 2.4 秒，一段 8 句的回复 ≈ **19 秒纯合成等待** |
| **影响** | 体验问题，**不是正确性问题**。回复短时无感，回复长时明显 |
| **为什么不修** | 属于 M3 的「边说边播」范畴 —— 单独修这一处只是打补丁，和 M3 的整体流水线改造重复 |
| **何时重做** | ✅ **M3 已解决**：`_SentenceSpeaker` 用 producer/worker 双线程，播放第 i 句时第 i+1 句已在合成。但 `reply()` 仍是「整段生成完才返回」，所以**首句出声时间没有改善** —— 那一半待做 |
| **审查证据** | 探针 M/O：4 句回复 → TTS 调用 4 次、播放 4 次，严格 1:1 无重叠 |

---

### KI-002 · 一次收到多句时合并成一段播报

| 项 | 内容 |
|---|---|
| **来源** | M2 独立审查 · G7 |
| **级别** | 🟢 建议 |
| **位置** | `app/capabilities/llm.py` · `split_sentence()` |
| **问题** | 缓冲区里有多句时，切到**最后一个**强标点，多句被合并成一段交给 TTS。这与「按句合成能在句间留出自然停顿」的注释有张力 |
| **影响** | 听感上句间停顿变少，但**换来了更少的 TTS 往返次数** |
| **为什么不修** | 这是**刻意的性能取舍**：切得更碎会让 TTS 往返次数线性增长（见 KI-001，单次约 2.4 秒）。当前取"少停顿、少往返" |
| **何时重做** | M3 只做了「合成与播放重叠」，`reply()` 未改成生成器，因此**维持现状**；等它真正流式化后再改为切第一个 |
| **备注** | `tests/test_text_pure.py::test_last_strong_mark_wins` 把当前行为固化为预期，改动时需同步更新该测试 |

---

### KI-003 · 视觉与工具调用尚未实现

| 项 | 内容 |
|---|---|
| **来源** | 项目规划 |
| **级别** | — （未实现，非缺陷） |
| **问题** | `qwen3-vl-plus` 视觉理解与天气工具调用在 README 中标注为 M5 |
| **影响** | 说「看看这是什么」不会有反应 |
| **何时重做** | M5 |

---

### KI-004 · VAD 在真实噪声环境下的表现未经实测

| 项 | 内容 |
|---|---|
| **来源** | M3 开发过程中的自我修正 |
| **级别** | 🟡 应修（验证缺口，非代码缺陷） |
| **位置** | \`app/perception/vad.py\` |
| **问题** | 我一度声称「中等白噪声下纯 VAD 100% 误判」，**这个结论是错的** —— 它来自不当的测试方法（同一 VAD 实例连调多次）。改用每帧新建实例后又得到互相矛盾的结果（sigma=0.002 判 40/40，sigma=0.01 却只判 3/40） |
| **根因** | **WebRTC VAD 依赖连续帧的历史**，孤立地单帧调用得不到有意义的结果。合成噪声无法用来评估它的判别力 |
| **当前状态** | M3 审查补测了**真实用法（单实例流式喂入）**，边界已经明确 |
| **已知边界** | 白噪声 3000 帧（60 秒），单实例流式：<br>
能量 261 → 0.0% 判为人声，分段器未启动<br>
能量 1045 → 0.2%，未启动<br>
**能量 1307 → 24.0%，最长连续 11 帧 → 分段器误启动**<br>
能量 1830 → 100%，持续误启动 |
| **结论** | 当背景噪声能量达到「正常说话区间」（300~3000）时会被误当人声。能量门挡不住，因为能量本身是达标的 |
| **影响** | 在嘈杂环境（开着电视、空调、马路）下可能自触发 |
| **何时重做** | 实际使用中出现自触发时。**白噪声不等于真实噪声**，真实环境仍需复测 |
| **备注** | 已把「不要用单帧噪声测 VAD」这条教训写进 vad.py 的模块文档 |

---

### KI-005 · 退出时麦克风关闭与输入线程竞争

| 项 | 内容 |
|---|---|
| **来源** | M3 审查 · X13（🟢 可以不管） |
| **位置** | `app/orchestrator/assistant.py` 的退出路径 × `app/audio/capture.py` 的 `close()` |
| **问题** | 读线程多半正阻塞在 `stream.read()`；此时关闭会让它抛 `AudioDeviceError`（被当成「麦克风读取中断」报错），若刚好在两次读之间则可能撞上 `assert self._stream is not None` |
| **影响** | 退出瞬间可能多打一行 ❌ 或 traceback。**纯观感，不影响功能** |
| **为什么不修** | 修它要引入关闭握手（event + join），复杂度与收益不成比例 |
| **何时重做** | 退出时真的看到 traceback，或 M4 的界面需要干净的退出提示 |

---

### ~~KI-006 · 编排层的事件定义重复、载荷不一致~~ ✅ M4 已解决

| 项 | 内容 |
|---|---|
| **来源** | M3 审查 · X14 / X15（🟢 可以不管） |
| **原问题** | ① 事件常量、`EventCallback`、`TurnResult` 各定义一份；② 同名事件 `tts_done` 的载荷不一致（pipeline 带 `total`，assistant 不带） |
| **何时重做** | 原计划「M4 做界面时一并处理」 |
| **✅ 结果** | M4-1 抽出 `app/orchestrator/events.py`：事件名 + 载荷契约 + `EventCallback` + 共用 `TurnResult` 全部收敛。assistant 的 `tts_done` 补上 `total` |
| **怎么防止再漂移** | 光靠「记得同步改」是靠不住的。新增 21 条契约测试，用 **ast 静态扫描**两个编排器的每一次 `_emit`，逐个比对载荷键；另有一条断言「定义了却从不发出的事件」—— 这条当场就抓出了 M3 遗留的死常量 `EVT_SPEECH_START` |
| **回归测试** | `tests/test_events_pure.py`（21 条） |

---

### ~~KI-007 · run() 不可重入等 API 层面小坑~~ ✅ M4 已解决

| 项 | 内容 |
|---|---|
| **来源** | M3 审查 · X16（🟢 可以不管） |
| **原问题** | ① `run()` 第二次调用抛 `InvalidTransition`（退出 → 待机），且异常在 `try` 之外、不属于 `AyuanError`；② `Speaker.stop()` 不闭锁，句间窗口发出的停止请求被 `play()` 入口的 `clear()` 吞掉 |
| **何时重做** | 原计划「M4 需要重新开始会话时」 |
| **✅ 结果** | ① `run()` 可重入：EXITING → IDLE 交给 `_begin_new_session()`，不再喂给转换表（同一会话内它确实非法，跨会话则是合法的重置语义）；并发调用明确报 `AyuanError`；`InvalidTransition` 归入 `AyuanError` 家族 ② `Speaker.stop()` 改为无条件闭锁，由 `play()` 入口消费或 `clear_stop()` 清除 |
| **回归测试** | `test_assistant_pure.py::TestRunReentrancy`、`test_audio_pure.py::TestSpeakerStopLatch`（7 条） |

---

### KI-008 · restart() 会在 GUI 线程同步阻塞最多 3 秒

| 项 | 内容 |
|---|---|
| **来源** | M4 独立审查 · R3（🟡） |
| **级别** | 🟡 应修 |
| **位置** | `app/ui/controller.py` · `restart()` / `apply_devices()`；调用方 `app/ui/tray.py`、`app/ui/settings_panel.py` |
| **问题** | `restart()` → `wait_stopped()` → `thread.join(3.0)`，而它由托盘 QAction 与设置面板按钮直接调用，都在 **GUI 线程**。会话正在 ASR/LLM 里时，实测阻塞 3.02 秒 |
| **影响** | 点一下界面冻结 3 秒，Windows 会把窗口标成「未响应」 |
| **为什么不修** | 会话几乎总是很快退出（`stop()` 置位后主循环一次迭代就结束），3 秒是**上限而不是常态**。真正的修法要引入「异步重启 + 完成回调」，把 `restart()` 从一个同步方法改成状态机 —— 对「换设备/重开会话」这种低频操作，复杂度与收益不成比例 |
| **何时重做** | 实测中真的出现「未响应」白屏时；或 M5 打包后发现冷启动本身就慢，值得一并优化 |
| **备注** | M5 打包后要走一次真实冷启动与换设备流程，顺便观察这里 |

---

### KI-009 · 错误提示会把 hint 显示两遍

| 项 | 内容 |
|---|---|
| **来源** | M4 作者自查 · A2（🟡） |
| **级别** | 🟡 应修 |
| **位置** | `app/ui/controller.py` · `_run()`；同源问题也在 `app/orchestrator/assistant.py`、`app/orchestrator/pipeline.py` |
| **问题** | `AyuanError.__str__` 会把 hint 拼进 message（`f"{message}（{hint}）"`），而各处又单独发一次 hint。界面上于是显示成：<br>`❌ 云端返回错误（HTTP 401）（API Key 无效或已过期）`<br>`API Key 无效或已过期` |
| **影响** | 报错框里同一句话出现两次，看着像程序坏了。**不影响功能** |
| **为什么不修** | 要改就得决定「`str(exc)` 到底该不该带 hint」——而这会牵动 M2/M3 就已在用的日志输出与三处异常分发。当前行为不算错，只是重复 |
| **何时重做** | 下次动错误展示时一并处理（改 `controller._run` 用 `exc.message` 而不是 `str(exc)` 即可，但要同步看另外两处） |

---

### KI-010 · .env 行级回写的几个边角

| 项 | 内容 |
|---|---|
| **来源** | M4 独立审查 · R6（🟢） |
| **级别** | 🟢 建议 |
| **位置** | `app/config.py` · `update_env_file()` |
| **问题** | 四个边角（审查者实跑记录）：<br>① **BOM 开头**的 .env 认不出已有键 → 追加一行**重复键**<br>② 无 BOM 的 **LF 文件**被整体改写成 CRLF<br>③ `export KEY=` 行的 `export ` 前缀被吃掉<br>④ 替换失败（只读文件）会留下含**完整内容（含密钥）**的 `<name>.tmp` |
| **影响** | 都不影响当前用法：真实 `.env` 无 BOM；`.env.tmp` 已被 `.env.*` 忽略；重复键需要有人手工写重复行 |
| **为什么不修** | 属于「承诺比实现更绝对」，不是故障。①③ 需要有人真的那么写文件；② 是 `splitlines()` 的固有行为 |
| **何时重做** | 真的收到「设置保存后启动异常」的反馈时 |
| **备注** | ④ 稍微值得留意：只读场景下会短暂在磁盘上留一份带密钥的副本。当前 `on_save` 已捕获 `OSError` 并提示，原文件不被破坏 |

---

### KI-011 · 另有三条 🟢 记录在案但不打算动

| 项 | 内容 |
|---|---|
| **来源** | M4 独立审查 · R7 / R8，以及作者自查的 app/ui 注释表述 |
| **级别** | 🟢 建议 |
| **问题** | ① **R7**：`SUBSCRIBED_EVENTS` 里 5 个事件没有专门信号，只进技术面板原始日志 —— 这是「事件日志留全量」的设计本意，**不是缺陷**<br>② **R8**：README 三处小出入 —— 说「内置 4 个调试配置」实际 11 个；说 `app/main.py` 是 F5 默认入口（M4 起默认是界面）；对比表写「asyncio + Qt 信号槽」而 `app/` 里 `asyncio` 只出现在 logger 静音名单<br>③ `app/ui/__init__.py` 自称「只有 controller.py 允许 import 编排层」，实际 theme/orb/tray/main_window 都 import 了 `states`（那是契约模块，正当使用） |
| **影响** | 读到的人会找错入口 / 误判技术栈；注释与代码不符 |
| **为什么不修** | ②③ 是文档措辞，不影响任何行为；① 本就符合设计 |
| **何时重做** | M5 收尾做「文档收口」时顺手改 —— 那一步本来就要通读一遍 README |

---
## 已修复（留存索引）

这些曾经是问题，已修复并有回归测试保护：

| 来源 | 问题 | 回归测试 |
|---|---|---|
| M1 审查 R1 | 麦克风读取无终止条件会永久挂死 | 人工桩测（`_MAX_EMPTY_READS`） |
| M1 审查 R2 | `rms()` 遇奇数字节抛裸 ValueError | `test_audio_pure.py::TestRms::test_never_raises` |
| M1 审查 Y10 | 完全没有测试 | 103 条测试 |
| M2 审查 B1 | 重试机制对网络异常完全失效 | `test_retry_pure.py`（13 条） |
| M2 审查 B2 | 计时分项之和 ≠ 合计 | `test_m2_review_pure.py::TestTimingSummary` |
| M2 审查 Y1b | ASR 解析漏出裸异常 | `test_m2_review_pure.py::TestAsrParseHardening`（14 组畸形结构） |
| M2 审查 Y3 | 清洗吞掉 snake_case 下划线 | `test_m2_review_pure.py::TestCleanForSpeechExtra` |
| M3 审查 X1 | 「停」的代码路径不可达（播报阻塞主线程） | `test_assistant_pure.py::TestInterrupt`（4 条） |
| M3 审查 X2 | 输入线程静默死亡导致永久挂死 | `test_assistant_pure.py::TestReaderTermination` |
| M3 审查 X3 | 分段器首尾音频重复 380ms | `test_perception_pure.py::TestSegmenterNoDuplication`（7 条） |
| M3 审查 X4 | 静默超时从不触发 | `test_assistant_pure.py::TestIdleTimeout` |
| M3 审查 X6 | `--threshold` 是静默空操作 | `test_assistant_pure.py::TestThresholdParameter` |
| M3 审查 X8 | 合成全空被当成成功 | `test_assistant_pure.py::TestEmptySynthesis` |
| M4 自查 | 打字发送后用户气泡出现两次（界面与事件流各记一笔） | `test_ui_pure.py::TestInputRow::test_sending_does_not_double_up_the_bubble` |
| M4 自查 | 光晕与涟漪溢出控件被裁成方形暗边 | `test_ui_pure.py::TestOrbFitsInsideItsWidget`（3 条） |
| M4 自查 | `timings["合计"]` 被存下来又被求和，合计虚高 | `test_events_pure.py::test_orchestrators_never_store_the_timing_total` |

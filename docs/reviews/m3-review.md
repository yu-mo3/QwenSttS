# M3 里程碑独立审查报告

> 审查对象：`feat/m3-perception` @ `fe039e1`
> 审查方式：独立子智能体，只读代码 + 实跑命令 + 自建探针，**未修改任何代码**
> 本文件为存档，流程见 [AGENTS.md](../AGENTS.md#一审查流程最重要的一条)

---

## 一、总体结论

**不能放行合并。**

M3 的三个核心承诺里有两个在代码层面根本不成立：

1. **「说『停』就停」的代码路径永远不会被执行** —— 实测整个会话中停用检测器被喂帧 **0 次**
2. **麦克风失效时助手静默挂死且无任何提示**
3. 另有一个：VAD 分段器送给 ASR 的音频**首尾各有 180~200ms 被重复拼接**

感知层的单元逻辑（8 个分段场景、唤醒率、状态机转换表）经独立复跑**全部通过**；
问题集中在 **M3 新增的编排层（`assistant.py`）没有一行测试**。

---

## 二、决策表格

| # | 级别 | 问题 | 不修会怎样 | 推荐 |
|---|---|---|---|---|
| X1 | 🔴 | `_tick()` 的 SPEAKING 分支不可达，主线程在 `_speak()` 里被 `join` 阻塞 | 「说停就停」完全不工作；用户喊破嗓子也没反应，只能等播完或 Ctrl+C | 🎯 必须修 |
| X2 | 🔴 | 输入线程正常结束（麦克风连续读空/设备失效）时无人处理，`_running` 仍为 True | 助手永久挂死：不报错、不退出、唤不醒，只能杀进程 | 🎯 必须修 |
| X3 | 🔴 | 分段器把前导缓冲与已收集帧重叠拼接，首 180ms 语音重复；尾部 200ms 也重复 | 每轮送进 ASR 的第一句开头被念两遍，首字识别抖动 | 🎯 必须修 |
| X4 | 🟡 | `_check_idle_timeout()` 只在帧队列 Empty 时调用 | 「静默 30 秒回待机」从不发生 | 💡 建议修 |
| X5 | 🟡 | 播报期间不消费帧队列，涨满后回灌给分段器 | 播报结束后先处理旧音频（含自己的喇叭声） | 💡 建议修 |
| X6 | 🟡 | `--threshold` 是静默空操作（monkeypatch 打在包命名空间） | 用户调阈值毫无效果，控制台还打印假的值 | 💡 建议修 |
| X7 | 🟡 | `requirements.txt` 里 sherpa-onnx / webrtcvad 仍是注释 | 新机器装完依赖，M3 直接 ImportError | 🎯 必须修 |
| X8 | 🟡 | 播报路径缺「一句都没合成出来」的失败判定（M2 有，M3 漏了） | 全句合成为空时被当成成功 | 💡 建议修 |
| X9 | 🟡 | `assistant.py` 零测试（142 条全在感知层） | 本次 3 个 🔴 全落在这个零覆盖的层里 | 💡 建议修 |
| X10 | 🟡 | 文档与代码不符；**验收标准 #3「首句出声明显快于 M2」不成立** | 按文档验收会得出错误结论 | 💡 建议修 |
| X11 | 🟡 | KI-004 的「VAD 真实表现未知」不准确：流式方法可测 | 一个可量化的风险被记成「未知」 | 💡 建议修 |
| X12 | 🟡 | 「停」的关键词从未真人验证，且 `wake_test.py` 测不了它 | 修好 X1 后可能再浪费一轮排查 | 💡 建议修 |
| X13 | 🟢 | 退出时 `microphone.close()` 与输入线程竞争 | 退出瞬间可能多一行 ❌，纯观感 | 🚫 可以不管 |
| X14 | 🟢 | 死代码：`record_after_wake`、`_speaker_thread`、被丢弃的 `TurnResult`、重复的事件常量 | 长期维护成本 | 🚫 可以不管 |
| X15 | 🟢 | 同名事件载荷不一致 | M4 的 UI 若依赖会 KeyError | 🚫 可以不管 |
| X16 | 🟢 | `run()` 不可重入、`Speaker.stop()` 不闭锁 | M4 若「重新开始会话」会拿到 traceback | 🚫 可以不管 |
| X17 | 🟢 | KI-001 / KI-002 未闭环说明 | 已知问题清单与现实脱节 | 🚫 可以不管 |

---

## 三、关键证据

### X1 · 「停」分支不可达

探针用假麦克风 + 假扬声器跑真实 `run()` 循环，播报 3.0 秒：

```
 0.000 run() begins
 0.000   [speaker] play start (3.0s)
 3.000   [speaker] play finished (full 3.0s)     <- 停 完全没有生效
RESULT stop_detector.feed calls total = 0        <- 一次都没被喂过帧
RESULT stats.interrupted            = 0
```

把状态强制成 SPEAKING 再直接调 `_tick()`：`speaker.stop_calls=1`、`stats.interrupted=1`
—— **分支逻辑本身是好的，问题在于主循环永远走不到它**。

**根因**：`_speak()` 写的是 `while thread.is_alive(): thread.join(timeout=0.2)`，
这是「分片 join」，**仍然阻塞主线程**。而 `_tick()` 只由主循环调用。
第 537-538 行的注释「主线程不 join，而是继续消费帧」与代码正好相反。

**连带后果**：播报 3 秒期间队列从 3 涨到上限 100（≈2 秒）并保持。
所以即使把分支接上，也必须**同时**改成边播边消费 ——
用户说「停」的那一帧在 2 秒后就会被丢掉。

**次级缺陷**：`player.stop()` 只在 `_playing` 已置位时才置 `_stop_event`，
而 `play()` 在入口会 `clear()` 它。句间窗口发出的停止请求会被静默丢弃，需要闭锁。

### X2 · 输入线程静默死亡

假麦克风的 `frames()` 产 20 帧后正常返回（模拟「连续读空判定设备失效」）：

```
run() returned after 3.06s -> 只因为外部 a.stop() 才退出
emitted events: ['state', 'exit']      <- 没有任何 error 事件
reader thread alive? False
EVT_ERROR emitted? False
```

去掉外部 `stop()`，这个 `run()` 就是无限循环。

**根因**：`capture.frames()` 连续读空后会**正常 return**，`_reader_loop` 直接返回；
但只有 `except AyuanError` 分支才置 `_running=False` + 发 `EVT_ERROR`。

### X3 · 分段器首尾重复

每帧带序号（20 帧静音 → 40 帧人声 → 40 帧静音）：

```
index sequence: [19, 20..28, 20..28, 29..79, 70..79]
DUPLICATED frame indices: [20..28] 与 [70..79]
duplicated count: 19 帧 = 380ms
```

**对照**：现有测试的桩帧内容全是 `b"\x00" * 640`，
所以重复**在测试里物理上不可能被发现**；`test_pre_buffer_kept` 断言 `frames > 30`，
重复后是 80，照样通过。

**根因**：确认说话前，同一帧被同时追加进 `collected` 和 `pre_buffer`；
确认时写 `collected = pre_buffer + collected`，同一批帧被拼了两遍。

### X11 · VAD 误启动边界（把「未知」变成「已知」）

单实例流式喂入 3000 帧（60 秒）白噪声：

| sigma | 能量均值 | 人声帧占比 | 最长连续人声帧 | 分段器 |
|---|---|---|---|---|
| 0.01 | 261 | 0.0% | 0 帧 | 未启动 |
| 0.02 | 522 | 0.1% | 4 帧 | 未启动 |
| 0.04 | 1045 | 0.2% | 5 帧 | 未启动 |
| **0.05** | **1307** | **24.0%** | **11 帧 (0.22s)** | **误启动** |
| 0.07 | 1830 | 100% | 3000 帧 | 持续误启动 |

**结论**：当背景噪声能量达到「正常说话区间」（300~3000）时，分段器会被连续噪声误启动。
能量门（阈值 300）挡不住这一档，因为能量本身是达标的。

> 白噪声不等于真实噪声（电视 / 空调 / 马路），真实环境仍需复测。

---

## 四、审查者确认做得好的地方

- **感知层的三条使用纪律在代码里真的守住了**：`vad.decide()` 每帧只调一次、
  `wake` / `stop_detector` / `vad` 确实是三个独立对象、唤醒后确实 `reset()`
- **状态机没有「卡死在 THINKING」的路径**（逐条走查每个出口）
- **分段器的 8 个声明场景用自己的桩独立复跑，全部通过**
- **`vad.py` 的实测记录「诚实且可复现」** —— 两张表逐格复现，两条关于
  webrtcvad 有状态的断言也成立。审查者的原话：
  > 「这是我在这个项目里见到的最诚实的一段文档。」
- **安全面干净**：全历史扫描无真密钥；日志无用户语音内容；
  `data/`、`*.wav`、`.env` 确实未被跟踪
- `run.bat` / `setup.bat` 纯 ASCII + CRLF 合规

---

## 五、审查方法

**完整读过**：`app/orchestrator/{assistant,states,pipeline,__init__}.py`、
`app/perception/{vad,segmenter,wakeword,__init__}.py`、`tools/{m3_assistant,wake_test}.py`、
`app/audio/{player,capture}.py`、`app/capabilities/{base,llm}.py`、
`app/core/{errors,logger}.py`、`app/main.py`、`README.md`、`KNOWN_ISSUES.md`、
`AGENTS.md`、`requirements*.txt`、`pyproject.toml`、`.gitignore`

**实跑命令**（全部退出码 0）：
```
pytest -o addopts="" -q          -> 142 passed
app.main --version / --devices   -> 正常
tools/check_env.py --no-record   -> 全过
tools/wake_test.py               -> 唤醒率 100%
tools/wake_test.py --scan        -> 0.02~0.2 触发 / 0.25+ 失效
tools/download_models.py --check -> 模型就绪
```

**自建探针 11 个**（`%TEMP%` 下，全用假麦克风/假扬声器/桩 VAD，不触发硬件）：
`m3_probe_speak` / `m3_probe2` / `m3_probe3` / `m3_probe_queue` /
`m3_probe_idle3` / `m3_probe_seg` / `m3_probe_8scenes` /
`m3_probe_vad` / `m3_probe_vad2` / `m3_probe_threshold` / `m3_probe_rerun`

**未覆盖**：真实麦克风、真实云端调用、真人「停」录音、外放回声场景 ——
相关结论均标注为「推断」。

---

## 六、附带发现（工具链）

`pyproject.toml` 的 `addopts = "-q"` 叠加命令行 `-q` 会变成 `-qq`，
导致 `pytest -q` 看不到统计行。需要 `-o addopts=""` 才能看到 `142 passed`。

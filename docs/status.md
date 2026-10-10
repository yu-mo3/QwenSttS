# 项目状态快照

> 把「截至目前的全部上下文」压成一页，供新会话或新协作者快速接上。
> 规则见 [AGENTS.md](../AGENTS.md)，未修问题见 [KNOWN_ISSUES.md](../KNOWN_ISSUES.md)。

---

## 一、这是什么

**阿源（QwenSttS）** —— 一个跑在 Windows 本地桌面上的多模态语音助手。
喊一声「阿源」唤醒，直接说话，VAD 自动判断你说完没有，她答完继续听下一句。

- 仓库：https://github.com/yu-mo3/QwenSttS （公开，MIT）
- 问题域参考《多模态智能语音助手开发教程》，但**架构与实现完全重写**
- 项目所有者：yu-mo3 —— **只有他能决定修什么**（见审查流程）

---

## 二、关键决策（已锁定）

| 项 | 决定 | 理由 |
|---|---|---|
| 运行环境 | Anaconda Python 3.12 独立虚拟环境 | **3.13+ 装不上** vosk / webrtcvad / pyaudio |
| 云端平台 | 阿里云百炼（DashScope） | 免费额度可用 |
| 对话模型 | `qwen3.8-27b`，**强制关闭思考** | 关思考首字 0.46s，不关 4.46s —— 差 10 倍 |
| 语音识别 | `qwen3-asr-flash-2026-02-10`（**原生**端点） | OpenAI 兼容端点不可用（SSL 层报错） |
| 语音合成 | `qwen3-tts-flash`，音色 Cherry 芊悦（**原生**端点） | 兼容端点的 `/audio/speech` 返回 404 |
| 视觉理解 | `qwen3-vl-plus` | 实测准确 |
| 唤醒方案 | sherpa-onnx KWS（**不是** Vosk+拼音匹配） | 「阿源」只有 2 音节，拼音匹配会被「原来/员工/原因」频繁误触发 |
| 唤醒词 | `ā y uán @阿源`，阈值 0.2 | token 写法是穷举试出来的，见第六节 |
| UI（M4） | PySide6，脸谱式主界面 + 右侧对话历史 | 用户选定 |
| 界面实现 | **QPainter 纯代码自绘**光球，零美术素材 | 项目没有任何图片资源，且四态要连续插值（切图会跳变） |
| Qt 依赖 | **PySide6-Essentials**，不是完整 PySide6 | 完整包多拖 WebEngine 131.7MB + Addons 40.5MB，一行都用不到 |
| UI 线程模型 | **命令进队列 / 事件出信号** | Qt 规定控件只能在 GUI 线程碰，而 L5 的回调来自三个线程 |
| 设置持久化 | 行级改写 `.env`，且**必须显式点按钮** | `.env` 里有密钥，静默改写它不可接受 |
| 音频路由 | 内置麦克风收音 + 蓝牙耳机放音 | 蓝牙耳机兼任麦克风会切免提模式，音质骤降 |
| 人设 | 女，知心知性、阳光开朗，外在大大咧咧、实则心思细腻 | 见 `app/capabilities/llm.py` 的 SYSTEM_PROMPT |
| 未来本地模型 | 预留 OpenAI 兼容接口（WSL2 + Ollama/vLLM） | 换 `base_url` 即可，上层不动 |

---

## 三、环境事实（本机实测）

- Python 3.12.4（`D:\Anaconda\python.exe`）· 虚拟环境 `.venv`
- 显卡 RTX 4060 Laptop 8GB · ffmpeg 已装
- 摄像头 USB2.0 HD UVC WebCam · 麦克风阵列 Realtek · 蓝牙耳机 Baseus Bass BF1 Lite
- **网络有 SSL 拦截**（SteamTools/Watt Toolkit 做中间人加速）：
  - Python 的 requests 不信任它 → GitHub 的 HTTPS 会失败
  - 但 `gh`（Go 写的）读系统证书库 → 正常工作
  - git 推送用 SSH over 443（`~/.ssh/config` 里配了 `ssh.github.com:443`）

---

## 四、进度

| 里程碑 | 内容 | 状态 |
|---|---|---|
| M1 | 工程骨架 · L1 基础层 · L2 设备层 · 环境自检 | ✅ 已完成（PR #1/#2/#3） |
| M2 | L4 能力层（ASR/LLM/TTS）· L5 单轮流水线 | ✅ 已完成（PR #4） |
| M3 | L3 感知层 · L5 状态机 + 连续对话 + 打断 + 边说边播 | ✅ 已完成（PR #5） |
| M4 | L6 表现层：PySide6 桌面界面 | ✅ 已完成（审查 🔴 已修，其余待定） |
| **M5** | **视觉理解 + Agent 工具池 + 打包 + 交接文档** | ⏳ **下一个** |

**测试**：372 条全部通过，`pytest -q` 直接跑。
**界面**：`python -m app.ui` —— 脸谱式光球 / 对话历史 / 技术面板 / 托盘 / 设置。
**审查**：每个里程碑完成后派独立子智能体审查，报告存档在 `docs/reviews/`。
M4 报告见 [reviews/m4-review.md](reviews/m4-review.md)：1 个 🔴 + 3 个 🟡 + 6 个 🟢。
**🔴 R1（有托盘时退出不退进程）已修**，其余等**项目所有者**决定 —— 未决定前不修。

---

## 五、分层架构

```
L6 表现层   app/ui/             app.py + main_window + orb + history + tech_panel
                                + settings_panel + tray + controller + bridge
L5 编排层   app/orchestrator/   events.py(契约) + pipeline.py(M2) + states.py + assistant.py(M3)
L4 能力层   app/capabilities/   base / asr / llm / tts
L3 感知层   app/perception/     vad / segmenter / wakeword
L2 设备层   app/audio/          capture / player / devices / wav
L1 基础层   app/core/ + app/config.py
```

**铁律**：只向下依赖；UI 不认识任何模型；业务代码不直接碰硬件；音频走内存不落盘。

---

## 六、踩过的坑（改动相关代码前务必先看）

### 唤醒词
1. **KWS 只认真人语音** —— TTS 合成的同一句话唤不醒（已用云端 ASR 交叉验证音频本身正常）。改动关键词后**必须真人复测**，无法自动化。
2. **token 写法是穷举试出来的**：关键词文件写的是「声母+韵母」，`ā y uán @阿源` 里的 `y` **必须写出来**。试过 `ā uán` / `ā yuán` / `ā üán` 等 10 种，只有这一种能唤醒。官方文档没说明。
3. 阈值实测区间 0.01~0.2（取 0.2），0.25 起失效。

### 感知层
4. **`webrtcvad` 与 sherpa-onnx 的流都携带内部状态** —— 同一帧连调两次结果不同；唤醒检测 / 停检测 / VAD 必须是**三个独立实例**，共用会互相污染。
5. **不要用单帧噪声测 VAD 的判别力** —— 它依赖连续帧历史，孤立调用无意义。曾因此得出「中等白噪声 100% 误判」的错误结论并写进文档。
6. VAD 能量门槛是**确定性**的（能量 < 300 一律不算语音），这半边的测试可以放心断言。

### 能力层
7. **百炼的语音能力不提供 OpenAI 兼容端点** —— 必须走原生 `/services/aigc/multimodal-generation/generation`。
8. **思考模式必须关**：通过 `extra_body` 传 `enable_thinking=False`。
9. **不要相信外部数据自报的大小** —— 百炼返回的「流式 WAV」把 size 字段填成 `0x7FFFFFxx`，直接采信会把时长算成 44739 秒。解析统一走 `app/audio/wav.py`。
10. 语音助手是在「说」不是「写」—— 输出必须清洗掉 Markdown / 表情符号，否则 TTS 会把星号念出来。清洗时**别吃 `snake_case` 的下划线**。

### 编排层
11. **播报绝不能阻塞主线程** —— 一旦阻塞，主线程回不到帧循环，「说停就停」就成了死代码（M3 审查发现的 X1）。
12. 输入线程结束**未必是异常**（`frames()` 读空到上限会正常 return），必须在 `finally` 里判定「为什么结束」，否则助手静默挂死。
13. 静默超时要**每帧**检查，不能只在队列空时检查。

### 表现层（M4）
18. **PySide6 + conda 会加载失败**：6.12 的 wheel 不再自带 ICU，而 conda 的
    `Library\bin\icuuc.dll` 只导出带版本后缀的 `ucnv_open_73`，Qt6Core 要的是
    无后缀的 `ucnv_open` —— 一共缺 20 个符号，整个 QtCore 加载失败。
    修法见 `app/ui/qt_runtime.py`（在任何 Qt import 之前预加载 System32 的 ICU）。
19. **跨线程刷 Qt 控件必崩，而且崩在随机位置**。槽函数**必须**连到 QObject 的
    绑定方法上；连 lambda 时 Qt 无从判断接收者归属，会退化成直连、
    在发射线程里执行。`bridge.gui_thread` 把这条变成第一次触发就报错的断言。
20. **光晕与涟漪会溢出控件被裁成方形暗边** —— 代码里完全看不出来，
    把四态渲染成 PNG 才发现的。现在球体尺寸从动效表**推导**出来
    （`MAX_RADIUS_RATIO = 1 / MAX_EXTENT_RATIO`），调幅度不会再撑出边界。
21. **offscreen 平台没有字体库**，中文会渲染成豆腐块。别把它当成「缺字形」——
    正式平台上实测 386 个字体、Microsoft YaHei UI 命中、「聆听中」宽 60px。
    离屏渲染只用来看版式，不看文字。
22. **打字输入必须由事件流驱动**：界面自己往历史里加一条，编排层随后又发
    `asr_done`，结果是两个一模一样的用户气泡。重复记账是最容易埋的一类 bug。
23. **不要让控件去猜数据的结构**（例如「从第几行开始是参数」）——
    控制器按**分区**返回 `[("设备", [...]), ("参数", [...])]`，
    耦合少一处，改错的机会就少一次。

### 工程
24. **`.bat` 必须纯 ASCII + CRLF**（cmd.exe 用 OEM 代码页读，UTF-8 中文会撕裂命令）。
25. **测试桩帧内容要可识别** —— 全是全零字节时，「同一帧被拼两遍」物理上看不出来。
26. **异常必须属于 `AyuanError` 家族** —— 裸异常会穿透上层容错。
27. **测试替身不许比真实实现宽松** —— FakeSpeaker 原先在 play() 入口无条件
    `clear()`，恰好把要修的 bug 演成了「预期行为」。
28. **窗口模态框会让离屏测试直接挂死** —— 清理阶段的 `close()` 会弹确认框，
    测试要嘛走「跳过确认」的真实路径，要嘛替换掉 QMessageBox。
29. **TypeScript 模板字符串里不能出现字面反引号** —— 记录文档时用单引号数组 join，
    否则工具链解析失败（本项目已踩多次）。同理 `${...}` 会被当插值。

---

## 七、工作约定（详见 AGENTS.md）

**审查流程**（重要）：
1. 每个里程碑完成后派独立子智能体审查（真实读代码 + 实跑命令）
2. **拿到报告后不得直接修** —— 先整理成决策表格（问题 / 不修会怎样 / 推荐）
3. **由项目所有者决定修什么**
4. 不修的记入 `KNOWN_ISSUES.md`，不能默默忽略

**提交约定**：分支 `feat/mX-描述` · Conventional Commits · 按 PR 合并保留完整历史
**安全**：密钥只放 `.env`（已 gitignore）· `_ref/` 不入库 · 日志不含用户语音

---

## 八、常用命令

```bash
.venv\Scripts\python.exe -m pytest -q               # 跑测试（372 条）
.venv\Scripts\python.exe -m app.ui                  # 图形界面（M4 起的主入口）
.venv\Scripts\python.exe -m app.ui --no-session -v  # 只开界面，调版式用
.venv\Scripts\python.exe tools\m3_assistant.py      # 免按钮语音对话（命令行入口）
.venv\Scripts\python.exe tools\m2_chat.py --text    # 纯文字对话（免麦克风）
.venv\Scripts\python.exe tools\check_env.py         # 环境自检
.venv\Scripts\python.exe -m app.main                # 项目体检面板
.venv\Scripts\python.exe tools\wake_test.py         # 唤醒词真人验证
.venv\Scripts\python.exe tools\download_models.py   # 下载唤醒模型（新机器）
```

---

## 九、下一步（M5）

**M4 已交付**（设计方案见 [design/m4-ui-design.md](design/m4-ui-design.md)）：
脸谱式光球（QPainter 自绘，四态各有动作语言）+ 对话历史气泡 + 文本输入
+ 技术面板（电平波形 / 设备 / 参数 / 事件日志）+ 设置面板（可写回 .env）
+ 系统托盘 + 关窗三选一。顺手收敛了 KI-006（统一事件契约）与 KI-007。

**M5 待办**：
- 视觉理解（说「看看这是什么」触发拍照 → `qwen3-vl-plus`）
- Agent 工具池（天气，`WEATHER_API_KEY` 已预留）
- 打包分发（PySide6-Essentials 已经为此把体积压下来了）
- **面向接手开发者的交接文档**（目标是「新人看完就能接手」）

**M5 开工前记得**：界面的设置面板已经占住了 `.env` 的三个键
（`AYUAN_WAKE_THRESHOLD` / `AYUAN_STOP_THRESHOLD` / `AYUAN_IDLE_TIMEOUT`），
新增配置项时沿用 `config.update_env_file()`，别另造一套写盘逻辑。

# M4 里程碑独立审查报告

> 审查对象：`feat/m4-ui` @ `7464234`
> 审查方式：独立子智能体，只读代码 + 实跑命令 + 自建探针，**未修改任何代码**
> 本文件为存档，流程见 [AGENTS.md](../../AGENTS.md#一审查流程最重要的一条)
>
> ⚠️ 审查者的探针脚本曾误落进仓库根目录（一个字面名为 `undefined` 的目录，
> 因其路径拼接时变量未定义）。已清理，未影响任何已提交内容。

---

## 一、总体结论

**不建议直接放行合并 —— 有 1 个 🔴 阻塞项，修掉即可放行。**

本轮技术含量确实高：372 条测试全绿、事件契约用 ast 静态扫描守住、光球几何是
**真·推导**（用稠密网格复核过）、分层与密钥纪律都站得住。缺陷不在「写得糙」，
而在一条没人测过的生命周期路径：**只要机器上「有」系统托盘（也就是正常
Windows 桌面），托盘菜单的「退出阿源」和关窗确认框的「退出」都不会让进程退出**
—— 窗口消失、托盘图标消失，但进程还活着，麦克风会话线程继续跑，
`aboutToQuit` 永不触发，于是 `controller.stop()` / `microphone.close()`
一次都没执行。这不是观感问题，是「以为关了其实没关」的资源与隐私问题，
而 README 里还写着「退出会立刻把麦克风还给系统」。

修这一条很小（退出分支里补一句 `quit()`），其余 3 个 🟡 都不阻塞。

---

## 二、决策表格

| # | 级别 | 问题 | 不修会怎样 | 推荐 |
|---|---|---|---|---|
| **R1** | 🔴 | 托盘可用时，「退出阿源」（托盘）与「退出」（关窗确认框）都不退出进程。`app.py` 设了 `setQuitOnLastWindowClosed(False)`，而全项目**没有任何一处**调用 `app.quit()` / `app.exit()` | 窗口与托盘图标都消失，进程、会话线程、麦克风全部留在后台；`aboutToQuit` 不触发 → `_shutdown` 不跑 → `controller.stop()` 与 `microphone.close()` 一次都不执行。界面上已经没有入口能退出，只能任务管理器杀。README「退出会立刻把麦克风还给系统」成为假承诺 | 🎯 **必须修** |
| R2 | 🟡 | `VoiceAssistant.run()` 在 `microphone.open()` 抛错后不走 `finally`，`_running` 永远停在 `True` | 该实例永久不可复用（第二次 `run()` 抛「助手已经在运行中」）；清理路径整体被跳过。当前 UI 因为每次 `start()` 都新建实例所以没暴露，但这是「碰巧没事」 | 💡 建议修 |
| R3 | 🟡 | 托盘「重新开始会话」与设置面板「应用设备并重开会话」会在 **GUI 线程**同步等待最多 3.02 秒（`restart()` → `wait_stopped()` → `join(3.0)`） | 会话正在 ASR/LLM 里时，点一下就冻结界面 3 秒，Windows 会给窗口标「未响应」 | 💡 建议修 |
| R4 | 🟡 | `test_ui_pure.py::test_signal_map_covers_every_subscribed_event` 是**空转测试**：名字承诺「覆盖每个订阅事件」，断言只有 `assert handled`（非空） | 14 个订阅事件里 5 个本来就没有专门信号，而只要还剩 **1** 个映射这条测试就绿。删掉 8/9 个映射照样全绿 —— 它给的是虚假信心，不是保护 | 💡 建议修 |
| R5 | 🟢 | `bridge.py` 模块文档与 `status.md` 第 19 条声称「连 lambda 时 Qt 会退化成直连、在发射线程执行」。**实测不成立**（见 E5） | 纪律的**理由**是错的。结论（槽连 QObject 方法 + `gui_thread`）仍值得保留，但下一个人照抄这句话会得出错误的 Qt 认知 | 💡 建议修（改措辞即可） |
| R6 | 🟢 | `update_env_file` 的四个边角（见 E6）：① BOM 开头的 .env 认不出键 → 追加一行**重复键**；② 无 BOM 的 LF 文件被整体改写成 CRLF；③ `export KEY=` 行的 `export ` 前缀被吃掉；④ 替换失败（只读文件）会留下含完整内容（含密钥）的 `<name>.tmp` | 都不影响当前用法：真实 `.env` 无 BOM、`.env.tmp` 被 `.env.*` 忽略、重复键需要有人手工写重复行。属于「承诺比实现更绝对」，不是故障 | 🚫 可以不管（建议把②④顺手记进 KNOWN_ISSUES） |
| R7 | 🟢 | `SUBSCRIBED_EVENTS` 里的 `wake / speech_start / speech_end / playback_start / playback_done` 没有专门信号，只进技术面板原始日志 | 界面本身对它们没有行为。这不是缺陷，是「事件日志留全量」的设计本意 | 🚫 可以不管 |
| R8 | 🟢 | 文档小出入：README 说「内置 4 个调试配置」，`launch.json` 实际 11 个；README 目录结构说 `app/main.py` 是「VSCode F5 就跑它」，但 F5 默认现在是界面；README 对比表写「本项目：asyncio 独立线程 + Qt 信号槽」，而 `app/` 里 `asyncio` 只出现在 logger 的静音名单里 | 读到的人会找错入口 / 误判技术栈 | 💡 建议修（顺手） |
| R9 | 🟢 | `app/ui/__init__.py` 自称「本包里**只有 controller.py** 允许 import 编排层」，实际 `main_window / orb / theme / tray` 都 import 了 `app.orchestrator.states` | 注释与代码不符，真正的边界（「不 import 能力层/设备层里的模型对象」）反而被这句话遮住 | 🚫 可以不管 |
| R10 | 🟢 | `SessionController.shutdown()`（投 `CMD_SHUTDOWN`）定义了却**无人调用**，是死代码；`settings_panel.py` 用 lambda 连接且没加 `@gui_thread`；`tray.on_state_changed` 碰 QSystemTrayIcon 也没加装饰器 | 前者恰好是 R1 的现成修法之一（这条干净退出路径已经写好了）；后两者当前不构成风险（都是 GUI 线程信号），但让「装饰器覆盖所有触控件槽」这句自我约定不成立 | 💡 建议修 |

---

## 三、关键证据

### E1 · 🔴 R1 主证据：真实 `app.main()` 下托盘「退出阿源」不退出进程

托盘在 offscreen 下恒为不可用，所以把 `AyuanTray.available` 强制为 True
（**这正是 `app.py` 里 `setQuitOnLastWindowClosed(False)` 的触发条件**），
然后跑**真实的** `app.main(["--no-session"])`，在 `MainWindow.show()` 之后
300ms 触发 `tray.on_quit()`，用看门狗线程在 8 秒后判定进程是否还活着：

```
.venv\Scripts\python.exe %TEMP%\rev_probe\probe_final.py
16:40:59 │ INFO │ app.ui.app │ --no-session：只开界面，不启动语音会话
RESULT: aboutToQuit/_shutdown 是否跑过 = {}
RESULT: 进程仍活着（只能靠 os._exit 收场）
```

同一脚本里还包了 `SessionController.stop` 做探针 —— **一次都没被调用**。

代码侧交叉印证（`grep` 全 `app/`）：`app.quit(` / `app.exit(` /
`QCoreApplication.quit` **零处命中**；`setQuitOnLastWindowClosed` 只出现在
`app/ui/app.py`；`aboutToQuit.connect(_shutdown)` 也只有一处。所以
`_shutdown`（唯一会 `controller.stop()` 的地方）只有「应用真的退出」时才跑，
而应用永远不会真的退出。

最小 Qt 语义复现（排除 MainWindow 干扰）：

```
app.setQuitOnLastWindowClosed(False); w.show(); w.close()
→ close 之后 500ms 事件循环仍在跑 -> 进程没有退出
→ FINAL {'aboutToQuit': False}
```

关窗确认框走的是同一个 `close()`，因此同样不退出。

**唯一覆盖到的测试**是 `test_quit_shuts_the_window_without_asking`，
它只断言 `window._closing is True` —— 锁的是「没有弹确认框」，不是
「进程退出了」。所以这个缺陷 372 条测试一条都拦不住。

**为什么开发时没被发现**：offscreen 平台 `QSystemTrayIcon.isSystemTrayAvailable()`
返回 False，于是 `app.py` 走 `else` 分支、**不设**
`setQuitOnLastWindowClosed(False)`，关窗即退出。
也就是说：**这个 bug 只在有真实桌面的机器上出现，
而自动化测试永远跑在没有桌面的环境里。**

### E2 · 🟡 R2：麦克风打开失败后 `_running` 卡住

```
a = VoiceAssistant(Config.load({})); a.microphone.open = boom   # 抛 AudioDeviceError
a.run()  → 抛 AyuanError: 无法打开麦克风（设备被占用）
  _running 仍然是 True   state = 待机
a.run()  → 抛: 助手已经在运行中
```

`self._running = True` 与 `microphone.open()` 都在 `try` **之外**。

### E3 · 🟡 R3：`restart()` 冻结调用线程 3.02 秒（实测）

```
会话线程在跑 = True
restart() 在调用线程上阻塞了 3.02 秒
   （STOP_TIMEOUT = 3.0 ）
```

调用点是托盘 QAction 与设置面板的按钮槽，都在 GUI 线程。

### E4 · 🟡 R4：空转测试的实证

```
SUBSCRIBED_EVENTS 条数 = 14
feed_event 里专门分支的事件 = 9
列了但只进原始日志（无专门信号）的 =
    ['playback_done','playback_start','speech_end','speech_start','wake']
```

而测试正文是 `assert handled, "至少该有一个事件被专门处理"`。
把 9 个 `elif` 删到只剩 1 个，这条测试依然通过。

### E5 · 🟢 R5：lambda 连接的实测结果与文档相反

从名为 `ayuan-speak` 的 `threading.Thread` 里 emit，四种连接方式各记一次
「当时是不是 GUI 线程」：

```
--- 发射线程 join 之后、事件循环还没跑，log = （空）---
--- 事件循环跑过之后 ---
    ('A', 'Recv.slot',  True)      # 绑定方法
    ('A', 'lambda',     True)      # lambda
    ('A', 'plain_func', True)      # 普通函数
    ('A', 'partial',    True)      # functools.partial
```

发射时**一个槽都没执行**，四种连接全部被排队到 GUI 线程。
本机 PySide6 `6.12.0`。

同时在真实 MainWindow 上验证纪律本身是生效的：从工作线程 emit 时
`window.on_state_changed` 在 GUI 线程执行；而**直接**跨线程调用
`window.on_interrupted()` 会被 `@gui_thread` 抓住并抛 RuntimeError。

结论分两半：**「槽在 GUI 线程执行」这件事是真的**（而且比文档说的更稳），
**「lambda 会退化成直连」这个理由是错的**。

### E6 · 🟢 R6：`.env` 回写的边角（实跑）

```
[crlf.env]     换行样式保住了 ✓
[bom.env]      after 里追加了一行重复的 AYUAN_WAKE_THRESHOLD
[notrail.env]  before=LF … after=全部 CRLF            # LF 文件被整体改写
[export.env]   'export AYUAN_WAKE_THRESHOLD=0.20' → 'AYUAN_WAKE_THRESHOLD=0.35'
[dup.env]      '=0.20 / =0.99' → '=0.35 / =0.99'      # 只改第一处
sanitize inject -> '0.2 DASHSCOPE_API_KEY=stolen'     # 换行注入被吃掉 ✓
[readonly]     PermissionError … 文件还在 ✓ 但目录里留下 ro.env.tmp（含密钥）
```

设置面板那条路径本身是安全的：`on_save` 捕获 `OSError` 并弹「保存失败」，
原文件未被破坏。

**测试不会污染用户真实 `.env`**（实测，不是读代码）：

```
真实 .env SHA256 测试前=2DDC74DA0FF2A761DD0EFDF46B05CFB573F87BC48440C1827264DF38651ECD7B
真实 .env SHA256 测试后=2DDC74DA0FF2A761DD0EFDF46B05CFB573F87BC48440C1827264DF38651ECD7B
内容是否被改: False
```

### E7 · 其余被核对与证实的断言

| 断言 | 核对方式 | 结果 |
|---|---|---|
| 372 条测试全部通过 | `pytest -q`（跑了两遍） | ✅ 372 passed |
| L6 不认识任何模型 | `grep` 硬件/云端库名于 `app/ui` | ✅ 仅 3 处命中，全是注释与异常文案 |
| 光球几何是推导出来的、不溢出 | 0.01 步长稠密网格复核 + 渲染取边界像素 | ✅ 最坏值 `2.24 @ (LISTENING, level=1.0, t=0.0)` 与 `MAX_EXTENT_RATIO` **完全相等**，未低估；渲染后控件边界内外同色，无方形暗边 |
| KI-007 两条修复有真回归测试 | 定点跑测试类 | ✅ 断言的是行为不是实现 |
| 契约测试 21 条 | `pytest --collect-only` | ✅ 21，与 KI-006 记录一致 |
| 无真密钥入库 | `git log --all -p`（21362 行）扫 `sk-[A-Za-z0-9]{16,}` | ✅ 只有占位与测试夹具；`.env` 从未被提交 |
| `_ref/` 确实被忽略 | `git check-ignore -v` | ✅ 全部命中 |
| 日志里没有用户语音 | `grep` 所有 `log.*(text\|reply\|transcript\|user_text)` | ✅ 零命中 |
| `.bat` 纯 ASCII + CRLF | 逐字节统计 | ✅ 两个脚本均合规 |
| 兼容性入口没回归 | 实跑三个入口 | ✅ 全部正常 |
| 托盘不可用时不把窗口藏起来 | 实跑 + 定点测试 | ✅ 「收进托盘」被禁用，关窗能正常退出 |

---

## 四、审查者确认做得好的地方

1. **光球的几何推导是真推导，不是「看起来像推导」。** 用 0.01 的 t 步长 ×
   0.02 的 level 步长把五个状态全跑了一遍，最大值与模块里的 `MAX_EXTENT_RATIO`
   **完全相等** —— 说明 24 个采样点的粗网格没有漏掉极值。再渲染一遍取边界像素，
   控件内外同色。
2. **`tests/` 绝不碰用户真实 `.env`。** 完整套件跑两遍，前后 SHA256 与 mtime
   完全一致。`env_path` 做成可注入、测试用 `tmp_path`。
3. **事件契约用 ast 静态扫描，而且扫得比一般写法严。** 同时认 `**kwargs` 与
   字典字面量；遇到 `**` 展开、非字面量键就 `pytest.fail` 而不是静默放过；
   还额外加了 `test_no_dead_event_constants`，当场抓出 M3 遗留的
   `EVT_SPEECH_START`。这条测试的价值在于**它能失败**。
4. **KI-007② 的 `Speaker` 闭锁设计得干净。** 三个语义各有名字、注释写清了
   「什么时候该清」，7 条回归测试覆盖四种情形。
5. **`qt_runtime` 的修补带反向保护。** 先检查 wheel 里有没有自带 ICU，
   有就直接返回不插手；非 Windows 直接返回；幂等；预加载失败只 `log.debug`。
6. **降级路径是真做了，不是写在文档里。** 托盘不可用 → 禁用「收进托盘」并保持
   关窗即退出；设备枚举失败 → 返回「无法枚举（…）」而不是让面板消失。
7. **仓库卫生与安全纪律是真的。** `.gitignore` 先于业务代码到位且有效；
   全历史扫不出真密钥；`.bat` 实测纯 ASCII + 全 CRLF。
8. **设置写盘的取舍写清楚了，而且执行了。** 「任何持久化都必须是一次明确的点击」
   落到按钮上；`sanitize_env_value` 吃掉 `\r\n` 防注入并有专门测试。

---

## 五、审查者没能覆盖的部分

1. **真人唤醒与真人语音端到端** —— KWS 只认真人语音，无法自动化。
2. **真实云端调用**（ASR/LLM/TTS 的实际往返与首字延迟）—— 没有花额度打真实请求。
3. **真实系统托盘** —— offscreen 下恒为不可用，R1 的「有托盘」分支是强制打通的。
   `setQuitOnLastWindowClosed(False)` 的语义与平台无关，因此结论可迁移；
   但**建议项目所有者在真机上点一次托盘「退出阿源」做最终确认**。
4. **真实麦克风/扬声器与设备热插拔**。
5. **性能与长稳**（30fps 重绘 + 16Hz 电平的 CPU 占用、常驻内存曲线）。
6. **打包（M5）** —— `qt_runtime` 修补在打包后是否仍适用，不在本轮范围。
7. **高分屏 / 多显示器 / 真实字体下的观感**。

---

## 六、审查者给出的「如果只做一件事」

在 `MainWindow.request_shutdown()` 和 `closeEvent` 的退出分支里，
`close()` 之后补一句 `QApplication.instance().quit()`；
`SessionController.shutdown()`（已写好却无人调用的 `CMD_SHUTDOWN` 路径）
可顺带在 `_shutdown` 里用上。补一条测试：**断言关窗后 `app.aboutToQuit`
确实触发** —— 现有测试只断言 `_closing`，所以拦不住这个缺陷。

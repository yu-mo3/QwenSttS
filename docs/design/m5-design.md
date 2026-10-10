# M5 设计方案 · 多模态 + 工具 + 交付

> 里程碑定义见 [status.md](../status.md#四进度)。
> 本文件在动工前定稿，用于「先出设计方案给用户确认」这一步。
> 确认人：项目所有者 yu-mo3 · 确认日期：____

---

## 一、目标与非目标

### 目标（三项，由项目所有者圈定）

1. **视觉理解** —— 说「看看这是什么」，摄像头拍一张交给 `qwen3-vl-plus` 描述
2. **Agent 工具池** —— 问天气会**真的去查**，而不是编一个
3. **打包分发** —— 一个能直接给别人跑的 Windows 包

### 非目标（明确划出 M5 边界）

| 不做 | 为什么 |
|---|---|
| 面向接手开发者的交接文档 | 项目所有者决定留给以后 |
| 视频流 / 连续视觉 | 一轮一张静态图足够回答「这是什么」 |
| 回声消除 / barge-in | 需要 AEC，独立课题 |
| 代码签名 / 安装包（MSI） | 签名要证书；M5 只做免安装的绿色包 |

---

## 二、动工前已验证的外部事实

设计压在这几个假设上，所以先花最小成本问清楚，而不是写完了才发现不成立。

### 2.1 `qwen3.8-27b` 关思考后**支持 function calling** ✅

```
用例 1  该调工具的问题（带 tools） -> tool_calls: True
        -> get_weather({"city": "北京"})          耗时 3.08s
用例 2  不该调工具的问题           -> tool_calls: False，直接文本作答   1.01s
用例 3  回填工具结果后继续         -> 「北京今天天气晴朗，气温18°C。」  0.47s
```

这是整个工具池设计的前提。**成立**，所以走真正的 function calling，
不做「听到天气关键词就查」那种假装成 Agent 的路由。

### 2.2 心知天气私钥直连**可用** ✅

```
now.json     HTTP 200  0.36s   {"text":"晴","temperature":"25"}
daily.json   HTTP 200  0.33s   [{date, text_day, text_night, high, low, precip, ...}]
life.json    HTTP 404  AP010005  「The requested api does not exist」
中文城市名   北京/上海/杭州/深圳/西安/成都 全部直接命中
```

- `daily` **带 `precip`（降水量）**，所以「明天要带伞吗」答得出来
- 生活指数 API 在当前套餐下不可用 —— **不设计依赖它的功能**

### 2.3 摄像头在线 ✅

`USB2.0 HD UVC WebCam`，状态 OK。

---

## 三、分层归属

### 3.1 分层图的变化

```
L6 表现层   app/ui/
L5 编排层   app/orchestrator/     + 工具调用循环
L4 能力层   app/capabilities/     asr / llm / tts / **vlm**
            app/agent/            **工具池**（注册表 + schema + 分发）
L3 感知层   app/perception/
L2 设备层   app/audio/ + **app/camera/**    摄像头与音频并列，都是设备
L1 基础层   app/core/ + config.py
```

**摄像头为什么单独成包而不是塞进 `app/audio/`**：`app/audio/` 的模块名与文档
处处假设「音频」，塞进一个 `camera.py` 会让那个包的语义变糊。
设备层有两个并列的物理设备域，比一个混合包更清楚。

### 3.2 工具池为什么是 `app/agent/` 而不是 `app/capabilities/tools/`

README 早已预留 `app/agent/`。它不只是「又一个外部服务」——
它还包含**注册表、参数 schema 生成、调用分发、超时与错误隔离**。
和 asr/llm/tts 那种「一个端点一个类」不是一回事，值得独立成包。
（若你更倾向收进 `app/capabilities/`，这一条我按你的意思改。）

---

## 四、子模块设计

### 4.1 `app/camera/`（L2）

```
camera/capture.py    Camera 类：open() / grab_jpeg() / close()
                     —— 与 Microphone 同构，设备索引漂移要重新解析
```

- 用 **opencv-python**：`cv2.VideoCapture` 抓帧 → 编码 JPEG
- 抓帧前先丢掉若干帧（摄像头刚打开时前几帧往往过暗/未就绪）
- **必须有终止条件**：`read()` 返回 False 时重试上限，绝不无限循环
  （与 `Microphone._MAX_EMPTY_READS` 同一条纪律）
- 返回**内存中的 JPEG 字节**，不落盘 —— 与音频同一条「内存优先」铁律

### 4.2 `app/capabilities/vlm.py`（L4）

走 OpenAI 兼容端点（与 LLM 相同），模型 `qwen3-vl-plus`。

```python
class VisionProvider:
    def describe(self, jpeg: bytes, prompt: str) -> VisionResult
```

- 图片以 **base64 data URI** 直传，不落盘、不依赖临时文件
- 需要**压缩到合理尺寸**：1080p 原图 base64 后可能上 MB，
  而 VLM 只需要看得清 —— 长边压到 1024 足够
- `VisionResult` 与 `Transcript` 同构：text + stats + 耗时

### 4.3 触发：怎么知道用户想「看」

**从 ASR 文本匹配，而不是加 KWS 关键词。** 理由：

- 「看看这是什么」是一句话的内容，不是唤醒词 —— 它是**对话的一部分**
- KWS 关键词的 token 写法要穷举试（status.md §六.2），加一个词就要真人复测一轮
- 文本匹配可以覆盖多种说法：`看看这是什么 / 这是什么 / 看看我手里 / 你看一下`

匹配到就把这一轮的输入**替换成**视觉描述 + 用户原话，再交给 LLM 组织回答。
触发词表放在 `app/orchestrator/` 的一个常量里，可单测。

### 4.4 工具池 `app/agent/`（L4）

```
agent/base.py       Tool 抽象：name / description / parameters(JSON Schema) / run(**kw)
agent/registry.py   注册表：注册、按名查找、导出 OpenAI tools 格式
agent/weather.py    天气工具（心知天气）
agent/errors.py     工具执行失败要变成「模型能看懂的结果」，而不是抛穿
```

**一条关键纪律**：工具**执行失败不能抛穿** —— 要把失败变成一条
`{"error": "..."}` 的工具结果回填给模型，让它自己决定怎么说。
否则「查天气超时」会让整轮对话失败，而用户其实只想听一句「查不到」。

### 4.5 工具调用循环（L5）

这是 M5 对 L5 最大的改动。现状 `ChatProvider.reply()` 是「一次流式生成完」，
工具轮次需要「生成 → 若有 tool_calls 则执行 → 再生成」。

```
第一次请求：带 tools 的**流式**请求
   ├─ 没有 tool_calls -> 与现在完全一样（首字延迟不受影响）
   └─ 有 tool_calls   -> 执行工具 -> 追加 assistant/tool 消息 -> 第二次流式请求
```

**性能取舍**：只有真的要调工具时才多一次往返。不调工具的普通聊天
**完全不增加延迟** —— 这是必须守住的，否则 M2 辛苦压到 0.46s 的首字就白费了。

工具执行要有**超时与并发上限**：模型理论上可以一轮要求调 10 个工具。

### 4.6 UI 接线（L6）

- **视觉结果卡片**：历史里显示缩略图 + VLM 的描述（图片只存内存，卡片显示缩略图）
- **工具调用可视化**：调工具时显示「正在查天气…」，而不是让她沉默 3 秒
- 两者都走新增事件：`EVT_TOOL_START` / `EVT_TOOL_DONE` / `EVT_VISION_DONE`
  —— 按 M4 的规矩，**同步更新 `events.py` 的契约表**，静态扫描会自动校验

### 4.7 打包（PyInstaller，onedir + 模型入包）

- `onedir`：启动快、出问题能直接翻目录排查
- 唤醒模型（约 31MB）打进包里，**首次运行不依赖网络**
- ⚠️ **必须验证 `qt_runtime` 的 ICU 修补在打包后是否仍适用** ——
  打包环境里可能既没有 conda 的旧 ICU、也没有 System32 的路径假设
- `.env` **不打进包**：分发出去的是 `.env.example` + 首次运行引导
- 预计压缩后 250~350MB

---

## 五、验证方案

### 自动化（新增测试）

| 测试 | 锁住什么 |
|---|---|
| `test_camera_pure.py` | 抓帧的终止条件、JPEG 编码、压缩比例 |
| `test_vlm_pure.py` | 响应解析加固（畸形结构不漏裸异常）、图片压缩、data URI 构造 |
| `test_agent_pure.py` | 注册表、schema 导出、工具参数校验、**工具失败变成结果而不是异常** |
| `test_tool_loop_pure.py` | 无 tool_calls 时不增加请求；有 tool_calls 时的轮次与消息序列 |
| `test_events_pure.py` 扩 | 新事件的载荷契约 |
| `test_ui_pure.py` 扩 | 视觉卡片与工具提示渲染 |

### 人工验收（必须真人做）

1. 说「看看这是什么」→ 拍一张 → 她描述出画面内容
2. 问「明天要带伞吗」→ 她真的调了天气工具并给出带降水的回答
3. 问「你好」→ **不调工具**，且首字延迟与 M4 相同
4. 打包后的 exe 双击能跑、能唤醒、能对话
5. 打包后**无网络**时能启动、能唤醒（模型已入包）

> ⚠️ 唤醒词仍无法自动验证（KWS 只认真人语音），第 4/5 条的唤醒部分要真人测。

---

## 六、风险与对策

| 风险 | 对策 |
|---|---|
| 工具调用让普通聊天的首字变慢 | 只有真调工具才多一次往返；写测试钉住「无 tool_calls 时请求数 == 1」 |
| VLM 图片太大导致请求超时/费用高 | 长边压到 1024 再 base64；实测一次调用的大小与耗时 |
| 摄像头在别人机器上打不开 | 与麦克风同一套优雅退化：报「无法打开摄像头」+ 可操作提示，不崩 |
| `qt_runtime` 修补在打包后失效 | 打包后**实测**能否启动；不行就换方案（随包带 ICU 或改用 PySide6 6.8） |
| PyInstaller 误报/漏收 Qt 插件 | onedir + `--collect-all PySide6`；实测「双击能起界面」而不是只看构建成功 |
| 打包体积失控 | 只用 PySide6-Essentials（M4 已经为此让路）；构建后报告实际体积 |

---

## 七、提交计划（每步推送，可独立验证）

| 步骤 | 内容 | 可独立验证的产出 |
|---|---|---|
| M5-0 | 本设计文档 | — |
| M5-1 | `app/camera/` + `app/capabilities/vlm.py` + 触发词 | 命令行拍一张并描述出来 |
| M5-2 | `app/agent/` 工具池 + 天气工具 | 命令行问天气得到真实数据 |
| M5-3 | L5 工具调用循环 | 无工具时请求数不变；有工具时结果正确 |
| M5-4 | 新事件 + UI 视觉卡片与工具提示 | 界面上看得见 |
| M5-5 | PyInstaller onedir 打包 + 模型入包 | 打包后双击能跑 |
| M5-6 | 文档收口 + **派独立子智能体审查** | 审查报告 + 决策表格 |

---

## 八、需要项目所有者确认的点

1. 摄像头用 opencv-python（+约 60MB）—— 已确认
2. 工具池走 function calling —— 已确认（且已实测可行）
3. onedir + 模型入包 —— 已确认
4. 交接文档不在 M5 范围 —— 已确认
5. **工具池放 `app/agent/` 还是 `app/capabilities/tools/`** —— 见 §3.2，倾向前者
6. **M5 是否要顺便修 M4 审查里那几条未决定的 🟡**（R2/R3/R4、A1/A2）—— 见 KNOWN_ISSUES / m4-review

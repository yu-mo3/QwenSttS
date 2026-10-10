<div align="center">

# 阿源 · QwenSttS

**一个能听、能说、能看的多模态语音助手**

喊一声「阿源」就能唤醒，自然地跟她聊天，她还能听懂你在说什么、看见你给她看的东西。

<sub>PySide6 · 阿里云百炼(Qwen) · 离线唤醒 · 流式对话 · 视觉理解 · 工具调用</sub>

![status](https://img.shields.io/badge/状态-开发中-orange)
![python](https://img.shields.io/badge/Python-3.11%20%7C%203.12-blue)
![license](https://img.shields.io/badge/license-MIT-green)

</div>

---

## 这是什么

一个跑在自己电脑上的**桌面语音助手**。和市面上的智能音箱最大的区别是：

- **数据不落地**：录音只在内存里流转，不写磁盘（原教程会先把录音存成固定路径的 mp3）
- **看得见、听得着**：不只有语音，还有摄像头视觉理解
- **随时能插话**：她说的时候你喊一声「停」，她立刻闭嘴听你说
- **可以换脑子**：云端模型和本地模型共用一套接口，换一行配置就能切

## ✨ 核心特性

| 能力 | 说明 | 状态 |
|---|---|---|
| ⚡ **流式生成** | 首字延迟 0.46~1.2s（实测）。首句出声 = 整段生成完 + 第 1 句合成，「边说边播」仍待做 | ✅ M2 / 🚧 待做 |
| 🗣️ **人设对话** | 会话记忆 + 分句播报，输出经清洗保证是「人话」 | ✅ M2 |
| 🎙️ **离线唤醒** | 喊「阿源」唤醒，sherpa-onnx KWS，不联网、低延迟 | ✅ M3 |
| ✂️ **自动断句** | VAD 判断你什么时候说完，**不用按键** | ✅ M3 |
| 🔁 **连续对话** | 唤醒一次就能一直聊，说「再见」或静默 30 秒才回待机 | ✅ M3 |
| 🛑 **随时打断** | 播报过程中喊「停」立刻停下 | ✅ M3 |
| ⚡ **边说边播** | 播放第 i 句时第 i+1 句已在合成，总时长显著缩短 | ✅ M3 |
| 🖥️ **图形界面** | 脸谱式光球随状态变化，右侧对话历史，可展开技术面板 | ✅ M4 |
| 👁️ **视觉理解** | 说「看看这是什么」，摄像头拍一张交给视觉大模型描述 | ⏳ M5 |
| 🌤️ **工具调用** | 问天气会真的去查天气，而不是编一个 | ⏳ M5 |

## 🚀 快速开始

### 1. 环境要求

| 项 | 要求 |
|---|---|
| 操作系统 | Windows 10 / 11（Linux 亦可，脚本需自行调整） |
| Python | **3.11 或 3.12** |
| 麦克风 | 必需 |
| 扬声器/耳机 | 必需 |
| 摄像头 | 可选（只影响视觉理解） |
| ffmpeg | 可选（音频格式转换用） |

> ⚠️ **不要用 Python 3.13 及以上**：`vosk`、`webrtcvad`、`pyaudio` 等库尚未提供对应的 wheel，装不上。

### 2. 初始化

双击 **`setup.bat`**，它会自动完成：

1. 定位 Python 3.11/3.12
2. 创建独立虚拟环境 `.venv`
3. 安装 `requirements.txt` 里的全部依赖

### 3. 配置密钥

1. 复制 `.env.example` 为 `.env`
2. 到 [阿里云百炼控制台](https://bailian.console.aliyun.com/) 申请 API Key，填入 `DASHSCOPE_API_KEY`

```ini
DASHSCOPE_API_KEY=sk-xxxxxxxxxxxxxxxx
```

> 🔐 `.env` 已被 `.gitignore` 屏蔽，**永远不会进入 git 仓库**。这是刻意的设计——密钥一旦进入 git 历史，即使后来删除也能被翻出来。

### 4. 自检

双击 **`run.bat`**，它会检查环境、列出音频设备、录 3 秒再放出来。

**听到自己刚才说的话 = 环境就绪。**

### 5. 启动

```bash
.venv\Scripts\python.exe -m app.ui      # 图形界面（推荐）
```

喊一声「阿源」把她叫醒，说完停顿一下她就知道你说完了。
不想用麦克风时，直接在输入框里打字也能聊 —— 这条路径**不经过唤醒词**，
排查「她怎么不搭理我」时特别有用。

关窗时会问一句「收进托盘还是退出」：收进托盘后她仍在待命，
喊一声还能唤醒；退出会立刻把麦克风还给系统。

## 💻 开发

### 在 VSCode 中启动（推荐）

用 VSCode 打开项目文件夹，按 **F5** 就能跑。项目已内置 4 个调试配置（`.vscode/launch.json`）：

| 调试配置 | 作用 |
|---|---|
| ▶ **启动阿源（界面）** | 运行 `app/ui/app.py` —— 平时就用这个 |
| 🖼️ **界面（不启动语音会话）** | 只开界面，不需要麦克风，调版式时用 |
| ▶ **启动阿源** | 运行 `app/main.py` —— 项目体检面板 |
| 🔍 **环境自检** | 运行五步自检（含录音回放） |
| 🔇 **环境自检（跳过录音）** | 没麦克风时用 |
| 🎧 **只看音频设备** | 列出设备与推荐路由 |

VSCode 首次打开会提示安装推荐扩展（Python / Pylance / debugpy），点安装即可；解释器已自动指向 `.venv`。

### 命令行方式

```bash
# 项目依赖虚拟环境，直接用系统 python 会找不到依赖。
# 可先激活：.venv\Scripts\activate    （或直接用下面的全路径写法）
.venv\Scripts\python.exe -m app.main            # 启动阿源
.venv\Scripts\python.exe -m app.main --check    # 环境自检
.venv\Scripts\python.exe -m app.main --devices  # 只看音频设备
.venv\Scripts\python.exe -m pytest              # 跑单元测试

# 语音对话（M2 起可用）
python tools/m2_chat.py         # 对着麦克风说话，她会用语音回你
python tools/m2_chat.py --text  # 纯文字模式（不录音，打字测试）
python tools/m2_chat.py --say "你好"   # 只跑一轮，用于自动化验证
```

### 三种入口的分工

| 入口 | 给谁用 |
|---|---|
| `app/ui/app.py` | **日常使用**：图形界面（VSCode F5 / `python -m app.ui`） |
| `tools/m3_assistant.py` | 免按钮语音对话的**命令行**入口，没有界面时排障最快 |
| `app/main.py` | 项目体检面板：配置摘要 / 里程碑进度 / 音频设备 |
| `run.bat` | 不想开 IDE，双击即用；将来打包给别人也是它 |
| `tools/check_env.py` | 独立的诊断工具，专门排查环境问题 |

## 🏗️ 架构

分层设计，每一层只依赖它下面的一层。业务代码不直接碰硬件，也不直接碰云服务。

```
┌──────────────────────────────────────────────────────────┐
│ L6 表现层    脸谱式光球 / 对话历史 / 技术面板 / 托盘        │
├──────────────────────────────────────────────────────────┤
│ L5 编排层    状态机：待机→唤醒→聆听→思考→说话→被打断      │
├──────────────────────────────────────────────────────────┤
│ L4 能力层    ASR │ LLM │ TTS │ VLM  （统一 Provider 接口） │
│              └── Agent 工具池（天气 / 时间 / …）           │
├──────────────────────────────────────────────────────────┤
│ L3 感知前端  VAD 断句 │ 关键词唤醒 │ 打断检测             │
├──────────────────────────────────────────────────────────┤
│ L2 设备层    麦克风采集（16kHz PCM）│ 扬声器播放（可打断）  │
├──────────────────────────────────────────────────────────┤
│ L1 基础层    配置 / 日志 / 异常                            │
├──────────────────────────────────────────────────────────┤
│ L0 运行环境  Python 3.12 虚拟环境 + 依赖清单               │
└──────────────────────────────────────────────────────────┘
```

### 三条设计铁律

1. **单向数据流** —— UI 只发命令、只订阅事件，不认识任何模型
2. **内存优先** —— 音频以字节流在层间传递，只有调试时才落盘
3. **接口隔离** —— 所有外部服务藏在 Provider 接口后面，换厂商只改配置

### 云端模型选型

两类端点并存，别搞混：

| 用途 | 端点 | 模型 |
|---|---|---|
| 对话 LLM | OpenAI 兼容 | `qwen3.8-27b` |
| 视觉理解 | OpenAI 兼容 | `qwen3-vl-plus` |
| 语音识别 | 百炼原生 | `qwen3-asr-flash-2026-02-10` |
| 语音合成 | 百炼原生 | `qwen3-tts-flash`（音色 `Cherry`） |

> ⚠️ **必须关闭思考模式**（`enable_thinking=False`）。实测同一条请求：开启思考首字延迟 **4.46 秒**，关闭后 **0.46 秒**。差 10 倍，语音助手会直接卡成 PPT。
>
> 百炼的语音合成与识别**不提供** OpenAI 兼容端点（`/audio/speech` 返回 404），必须走原生 \`multimodal-generation\` 接口。

## 📁 目录结构

```
QwenSttS/
├─ app/
│  ├─ main.py              # 应用主入口（VSCode F5 就跑它）
│  ├─ config.py            # 全局配置（.env + 默认值）
│  ├─ core/                # L1 基础层
│  │  ├─ errors.py         #   统一异常类型
│  │  └─ logger.py         #   日志
│  ├─ audio/               # L2 设备层
│  │  ├─ devices.py        #   设备枚举与按关键字选取
│  │  ├─ capture.py        #   麦克风采集（定长 + 流式）
│  │  ├─ wav.py            #   WAV 编解码（处理「流式 WAV 假 size」）
│  │  └─ player.py         #   扬声器播放（可中途打断）
│  ├─ capabilities/        # L4 能力层
│  │  ├─ base.py           #   HTTP 客户端 / 重试 / 调用统计
│  │  ├─ asr.py            #   语音识别（百炼原生端点）
│  │  ├─ llm.py            #   对话（流式 + 分句 + 人设）
│  │  └─ tts.py            #   语音合成（百炼原生端点）
│  ├─ orchestrator/        # L5 编排层
│  │  ├─ events.py         #   事件契约：L5 与 L6 之间唯一的约定
│  │  ├─ pipeline.py       #   单轮流水线（M2）
│  │  ├─ states.py         #   状态机定义（M3）
│  │  └─ assistant.py      #   常驻助手：连续对话 + 打断 + 边说边播（M3）
│  ├─ perception/          # L3 感知层
│  │  ├─ vad.py            #   人声判定（WebRTC VAD + 能量双条件）
│  │  ├─ segmenter.py      #   语音段切分（起止判定 + 前后缓冲）
│  │  └─ wakeword.py       #   关键词唤醒（sherpa-onnx KWS）
│  ├─ ui/                  # L6 表现层（M4）
│  │  ├─ app.py            #   入口：python -m app.ui
│  │  ├─ main_window.py    #   主窗口装配
│  │  ├─ orb.py            #   脸谱式光球：四态各有各的动作语言
│  │  ├─ history.py        #   对话历史气泡（模型与控件分开，模型可单测）
│  │  ├─ tech_panel.py     #   电平波形 / 设备 / 参数 / 事件日志
│  │  ├─ settings_panel.py #   设备与感知参数，可显式写回 .env
│  │  ├─ tray.py           #   系统托盘（图标按状态变色）
│  │  ├─ controller.py     #   唯一认识 L5 的地方，会话跑在后台线程
│  │  ├─ bridge.py         #   事件 -> Qt 信号，跨线程安全投递
│  │  ├─ theme.py          #   配色 / 尺寸 / QSS（不 import Qt，可单测）
│  │  └─ qt_runtime.py     #   conda 环境下的 ICU 加载修补
│  └─ agent/               # L4 工具池      （M5）
├─ .vscode/                # 调试配置（团队共享，已入库）
├─ tools/
│  ├─ check_env.py         # M1 环境自检脚本
│  ├─ m2_chat.py           # M2 语音对话验收脚本
│  ├─ m3_assistant.py      # M3 免按钮对话验收脚本
│  ├─ record_sample.py     # 录音采样（唤醒词验证用）
│  ├─ wake_test.py         # 唤醒词真人验证 + 阈值扫描
│  └─ download_models.py   # 下载唤醒模型（约 31MB，不入库）
├─ models/                 # 离线模型（不入库，运行时下载）
├─ data/                   # 运行产物（不入库）
├─ .env.example            # 密钥模板
├─ requirements.txt
├─ setup.bat               # 一键初始化
└─ run.bat                 # 一键自检
```

## 🗺️ 路线图

| 里程碑 | 内容 | 状态 |
|---|---|---|
| **M1** | 工程骨架 · L1 基础层 · L2 设备层 · 环境自检 | ✅ 已完成 |
| **M2** | L4 能力层：语音识别 → 对话 → 语音合成，命令行闭环 | ✅ 已完成 |
| **M3** | L3 感知层 + L5 编排层：离线唤醒、VAD 断句、连续对话、打断 | ✅ 已完成 |
| **M4** | L6 表现层：PySide6 桌面界面 | ✅ 已完成 |
| **M5** | 视觉理解 + Agent 工具池 + 打包分发 | ⏳ 计划中 |

## ❓ 常见问题

<details>
<summary><b>为什么语音合成的音频时长会算成 44739 秒？</b></summary>

这是踩过的一个真实深坑，值得单独记一笔。

百炼 TTS 返回的是**「流式 WAV」**：它的 `RIFF` 和 `data` chunk 的 size 字段被填成 `0x7FFFFFxx`（表示「写入时大小未知」），而不是真实字节数。

Python 标准库的 `wave` 模块**会照单全收**：

```
getnframes()                 ->  1073741773        (十亿量级)
getnframes() / getframerate() ->  44739 秒          ← 时长显示成 12 小时
readframes(getnframes())      ->  去申请 2GB 缓冲区  ← 内存炸弹
```

**对策**：`app/audio/wav.py` 是项目中唯一解析 WAV 头的地方，它**只信任 `fmt` chunk**，音频数据一律按「实际剩余字节」截断。

> 教训：**永远不要相信外部数据自报的大小。**
</details>

<details>
<summary><b>装好 PySide6，一 import 就报 <code>DLL load failed while importing QtCore</code>？</b></summary>

如果你用的是 **Anaconda 系的 Python**，多半会撞上这个。

PySide6 6.12 的 Windows wheel **不再自带 ICU**，Qt6Core.dll 于是直接
import 系统里的 `icuuc.dll`。而 conda 会把 `<conda>\Library\bin` 塞进
DLL 搜索路径，那里那份 ICU 只导出**带版本后缀**的符号：

```
<conda>\Library\bin\icuuc.dll    只有 ucnv_open_73
C:\Windows\System32\icuuc.dll     有无后缀的 ucnv_open   ← Qt 要的是这个
```

用 PE 导入表逐个符号比对，Qt6Core 一共缺 20 个符号，全是 `ucnv_*`
（编码转换器）。于是整个 QtCore 加载失败。

**对策**：`app/ui/qt_runtime.py` 会在任何 Qt import 之前把 System32 的 ICU
预加载进进程 —— Windows 的 loader 按模块基名复用已加载的 DLL。
在不需要它的机器上，这个函数只做一次目录检查就返回，没有副作用。
</details>

<details>
<summary><b>为什么不用 pyaudio 而用 sounddevice？</b></summary>

`sounddevice` 直接支持 numpy 数组、回调式低延迟采集，而且能优雅地按块写出实现「随时打断」。`pyaudio` 是教程里的方案，功能上够用，但在流式场景下丢帧会被静默吞掉。
</details>

<details>
<summary><b>蓝牙耳机听起来像打电话一样闷？</b></summary>

这是蓝牙协议本身的问题：蓝牙耳机一旦兼任麦克风，Windows 会把它切到「免提模式」(Hands-Free)，带宽骤降。

**解法**：让内置麦克风收音、耳机只负责放音。运行 `run.bat` 会给出推荐路由。
</details>

<details>
<summary><b>设备列表里为什么同一个设备出现好几次？</b></summary>

Windows 有 MME / DirectSound / WASAPI / WDM-KS 四套音频接口，同一硬件在每套下都会注册一次。本项目默认只显示 MME 的设备，加 `--all-devices` 可查看全部。
</details>

<details>
<summary><b>这个项目和市面上的教程有什么关系？</b></summary>

本项目的**问题域**参考了《多模态智能语音助手开发教程》（基于 PyQt5 的大模型应用开发），但**架构与实现完全重写**：

| 维度 | 原教程 | 本项目 |
|---|---|---|
| 组织方式 | 9 章 9 个独立脚本 | 7 层架构，能力按层归位 |
| 数据传递 | 固定路径磁盘文件 | 内存字节流 |
| 密钥 | 硬编码在源码里 | `.env` + `.gitignore` |
| 线程模型 | 主线程阻塞（UI 会卡死） | asyncio 独立线程 + Qt 信号槽 |
| 对话记忆 | 无 | 会话级上下文 |
| 打断 | 无 | 支持 |
</details>

## 📚 项目文档

| 文件 | 内容 |
|---|---|
| [AGENTS.md](AGENTS.md) | 协作约定：审查流程、分层铁律、编码与提交规范 |
| [KNOWN_ISSUES.md](KNOWN_ISSUES.md) | 已知问题：审查发现但决定暂不修复的项，含原因与重做时机 |

## 📄 许可

[MIT](LICENSE) © yu-mo3

</div>

# tests/

纯函数单元测试。**不需要任何硬件、网络或密钥**，随时可跑：

```bash
.venv\Scripts\python.exe -m pytest
```

## 为什么这些测试值得存在

它们锁住的都是**已经踩过的坑**，不是想象中的问题：

| 测试 | 锁住的坑 |
|---|---|
| `test_wav_pure.py::test_streaming_wav_with_bogus_size` | 百炼返回的「流式 WAV」把 data size 写成 `0x7FFFFFxx`，直接采信会把时长算成 44739 秒 |
| `test_audio_pure.py::TestRms::test_never_raises` | `np.frombuffer` 遇到奇数字节抛原生 `ValueError`，不属于 `AyuanError` 家族，上层兜不住 |
| `test_audio_pure.py::TestPlay::test_too_short_beep` | `beep(duration=0.0001)` 时淡入淡出数组无法广播，实测崩溃 |
| `test_config_pure.py::test_mask_never_leaks_body` | 密钥脱敏曾把首 8 位 + 末 4 位打到控制台，而自检输出正是用户求助时最爱截图的内容 |

**原则：每个修掉的 bug，都补一条测试。**

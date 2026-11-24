# CoinFlow

CoinFlow 是一个 Android 手机任务工具。它用 ADB 连接手机，用 scrcpy 投屏，并通过 OCR 识别任务页面和奖励。当前支持汽水音乐、抖音极速版和快手极速版。

- 在 Windows 控制台选择设备、查看投屏并运行任务。
- 查看任务进度和金币变动。日志和 SQLite 收益记录默认保存在本机 `logs/`。
- 可选用本地 Ollama 辅助识别异常页面。在本地 `.env` 设置 `DROIDFLEET_MODEL` 和 `DROIDFLEET_OLLAMA_URL`。

## 快速使用

1. 安装 Python 3、ADB 和 scrcpy，确保命令可以在终端运行。手机开启 USB 调试或无线调试，并通过 `adb devices` 确认已连接。
2. Windows 下安装构建依赖 `python -m pip install pillow pyinstaller`，运行 `build_gui.cmd`，然后打开项目根目录的 `CoinFlow.exe`。也可以运行 `python droidfleet_gui.py` 直接启动控制台。
3. 命令行入口为 `soda.py`、`douyin_rewards.py` 和 `kuaishou_rewards.py`。在对应命令后加 `--help` 查看用法。

[English](README.md)

# AI Passport Codex 语音控制器

使用 FoloToy AI Passport（ESP32-C3）内置麦克风，经 Wi-Fi 将声音送入 Windows 虚拟麦克风，由 Codex 原生听写转文字。

<p align="center">
  <img src="assets/code-cursor-screen.jpg" alt="Code Cursor: Let's talk." width="240">
</p>

*待机界面预览*

## 操作

- 单击 OK 发送；双击 OK 打断。
- 长按 OK 0.45 秒听写，松开结束，最长 30 秒。
- 短按上下键在松开时切换最近聊天（Ctrl+F7 / Ctrl+F8）。
- 长按上下键滚轮滚动，松开停止；鼠标需位于回复正文上。
- 仅 Codex 位于前台时发送输入。

## 配置

1. Windows 安装 Python，执行 `pip install -r requirements.txt`。
2. 准备 Steam Streaming Microphone 虚拟音频设备，播放和录音端均设为单声道、48000 Hz、16 位。Codex 选择其录音端。（也可使用其他虚拟声卡替代，但需同步修改桥接程序中的音频设备名称，并确保播放端与录音端格式一致。）
3. Codex 按住听写设置为 Ctrl+Shift+D，最近聊天切换设为 Ctrl+F7 / Ctrl+F8；具体设置依赖应用版本。
4. 将 `wifi-host.example.json` 复制到 `passport-backup/wifi-host.json`，填写电脑局域网 IPv4 和随机配对密钥，可用 `python -c "import secrets; print(secrets.token_hex(32))"` 生成。
5. 将 `ai-passport-main/main/passport_wifi_config.example.h` 复制到同目录的 `passport_wifi_config.h`，填写 2.4 GHz Wi-Fi、相同的电脑地址与密钥。
6. 激活 ESP-IDF 5.5.3，执行 `idf.py -C ai-passport-main build`。烧录前核对具体设备；现有保留分区方案仅向 0x10000 写应用，不覆盖出厂数据。
7. 防火墙允许可信局域网访问 Python TCP 8765，执行 `python passport_wifi_bridge.py`，建议固定电脑局域网地址。

虚拟声卡和快捷键需手动配置。编译固件包含 Wi-Fi 密码及配对密钥，请勿分享。

## 注意

请在可信局域网使用，音频传输未加密。桥接不保存日常录音，Codex 的录音保留由应用管理。
“Audio sent to Codex” 是定时提示，不代表转写已完成。

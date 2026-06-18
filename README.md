<div align="center">

# 📼 QQ 防撤回

<i>🍃 声落有声，影过留影</i>

![License](https://img.shields.io/badge/license-AGPL--3.0-green?style=flat-square)
![Python](https://img.shields.io/badge/python-3.10+-blue?style=flat-square&logo=python&logoColor=white)
![AstrBot](https://img.shields.io/badge/framework-AstrBot-ff6b6b?style=flat-square)

</div>

## ✨ 简介

一款为 [**AstrBot**](https://github.com/AstrBotDevs/AstrBot) 设计的 QQ 防撤回插件，支持对多种消息类型的监控与恢复，包括文字、图片、语音、视频、文件、聊天记录以及小程序等。

---

## 📖 使用须知

| 项目               | 描述                                                                                                                       |
| :----------------- | :------------------------------------------------------------------------------------------------------------------------- |
| **支持平台** | 仅支持 **`aiocqhttp`** 平台。                                                                                             |
| **监控范围** | 仅支持 **群聊** 消息的撤回监控。                                                                                      |
| **消息类型** | 支持聊天场景的所有消息类型。 |

> [!CAUTION]
> 如果会话配置了语音转文本，可能会导致语音消息的撤回监控无法正常工作。

---

## 🎮 指令

> 以下指令仅限 **管理员** 使用

| 指令 | 参数 | 描述 |
| :--- | :--- | :--- |
| **`撤回转发`** | `群号` `目标会话` | 为指定群设置转发目标。格式：`@数字` (私聊), `#数字` (群聊)。支持多次设置以转发到多个目标。 |
| **`取消撤回转发`** | `群号` `[目标会话]` | 取消指定群的转发目标。如果不带目标参数，则重置该群回默认转发配置。 |
| **`查看撤回转发`** | 无 | 查看当前所有自定义的撤回转发配置。 |

---

## ⚙️ 配置

首次加载后，请在 AstrBot 后台 -> 插件 页面找到本插件进行设置，所有配置项都有详细的说明和介绍。

---

## 📅 更新日志

详见 [CHANGELOG](CHANGELOG.md)

---

## ❤️ 支持

* [AstrBot 帮助文档](https://astrbot.app)
* 如果您在使用中遇到问题，欢迎在本仓库提交 [Issue](https://github.com/Foolllll-J/astrbot_plugin_anti_revoke/issues)。

---

<div align="center">

**如果本插件对你有帮助，欢迎点个 ⭐ Star 支持一下！**

</div>
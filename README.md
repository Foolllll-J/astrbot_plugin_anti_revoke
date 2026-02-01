<div align="center">

# 🛡️ QQ 防撤回插件

<i>👀 BIG BROTHER IS WATCHING YOU!</i>

![License](https://img.shields.io/badge/license-AGPL--3.0-green?style=flat-square)
![Python](https://img.shields.io/badge/python-3.10+-blue?style=flat-square&logo=python&logoColor=white)
![AstrBot](https://img.shields.io/badge/framework-AstrBot-ff6b6b?style=flat-square)

</div>

## ✨ 简介

一款为 [**AstrBot**](https://github.com/AstrBotDevs/AstrBot) 设计的高可靠性 QQ 防撤回插件，支持对多种消息类型的监控与恢复，包括文字、图片、语音、视频、文件、聊天记录以及小程序等。

---

## 📖 使用说明

| 项目               | 描述                                                                                                                       |
| :----------------- | :------------------------------------------------------------------------------------------------------------------------- |
| **支持平台** | 仅支持 **`napcat`** 平台。                                                                                             |
| **监控范围** | 仅支持 **群聊** 消息的撤回监控。                                                                                      |
| **消息类型** | 支持所有聊天场景的消息类型。 |

---

## ⚙️ 配置说明

| 配置项                              | 类型          | 默认值  | 描述                                                         |
| :---------------------------------- | :------------ | :------ | :----------------------------------------------------------- |
| **`monitor_groups`**        | `list[str]` | `[]`  | 要监控撤回事件的群号列表。                                   |
| **`target_receivers`**      | `list[str]` | `[]`  | 接收防撤回提醒的目标**QQ 号**。                     |
| **`target_groups`**         | `list[str]` | `[]`  | 接收防撤回提醒的目标**群号**。                     |
| **`ignore_senders`**        | `list[str]` | `[]`  | 忽略这些 QQ 号的撤回消息，不会触发转发。             |
| **`cache_expiration_time`** | `int`       | `300` | 消息缓存时间，单位: 秒。超过此时间的消息，撤回后将无法恢复。 |
| **`file_size_threshold_mb`** | `int`       | `300` | 视频和文件大小阈值，单位: MB。超过此大小的视频和文件将不会被缓存（设为 0 表示不限制）。 |
| **`forward_to_self`** | `bool` | `false` | 是否转发合并转发消息给机器人自身。开启后，收到的合并转发消息会自动转发给机器人自身私聊。 |
| **`forward_relay_group`** | `str` | `""` | 中转群号，用于转发合并消息起到缓存作用。 |
| **`auto_recall_relay`** | `bool` | `True` | 是否自动撤回中转群的消息。 |

---

## ⚠️ 注意事项

* **语音转文本**：如果会话配置了语音转文本，可能会导致语音消息的撤回监控无法正常工作。

---

## 📅 更新日志

<details>
<summary>点击展开更新日志</summary>

**v1.1.5**

- 更改图片、语音发送方式

**v1.1.4**

- 修复 Json 组件（小程序等）处理时可能出现的字典类型错误

**v1.1.3**

- 新增支持将合并转发消息转发给机器人自身

**v1.1.2**

- 修复在docker环境下可能遇到的文件权限问题

**v1.1.1**

- 新增被管理员撤回时显示撤回者

**v1.1**

* 新增支持将撤回通知发送到群聊

**v1.0**

* 新增支持合并转发和语音类型的消息。

**v0.3**

* 新增视频和文件大小阈值配置

**v0.2**

* 新增支持艾特、系统表情、视频、文件、json(小程序)类型的消息

**v0.1**

* 支持监控文本和图像消息

</details>

---

## ❤️ 支持

* [AstrBot 帮助文档](https://astrbot.app)
* 如果您在使用中遇到问题，欢迎在本仓库提交 [Issue](https://github.com/Foolllll-J/astrbot_plugin_anti_revoke/issues)。

---

<div align="center">

**如果本插件对你有帮助，欢迎点个 ⭐ Star 支持一下！**

</div>
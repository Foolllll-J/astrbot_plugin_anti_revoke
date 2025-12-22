# 🛡️ AstrBot 防撤回插件 (Anti-Revoke)
![License](https://img.shields.io/badge/license-GPL--3.0-green)
![Python](https://img.shields.io/badge/python-3.10+-yellow.svg)
![AstrBot](https://img.shields.io/badge/framework-AstrBot-blue)

一款为 [**AstrBot**]([AstrBot](https://github.com/AstrBotDevs/AstrBot)) 设计的高可靠性防撤回插件。

---

**<div align="center">**    <h2>👀 BIG BROTHER IS WATCHING YOU!</h2> </div>
------------------------------------

## ⚠️ 注意事项

| 项目               | 描述                                                                                                                       |
| :----------------- | :------------------------------------------------------------------------------------------------------------------------- |
| **支持平台** | 仅适配 **`napcat`** 平台。                                                                                             |
| **监控范围** | 仅支持 **群聊** 消息的撤回监控。                                                                                      |
| **消息类型** | 支持文本、图片、表情、视频、文件、JSON(小程序)及合并转发消息（需配置中转群）。 |

## ⚙️ 配置说明

| 配置项                              | 类型          | 默认值  | 描述                                                         |
| :---------------------------------- | :------------ | :------ | :----------------------------------------------------------- |
| **`monitor_groups`**        | `list[str]` | `[]`  | 要监控撤回事件的群号列表。                                   |
| **`target_receivers`**      | `list[str]` | `[]`  | 接收防撤回提醒的目标**QQ 号**。                     |
| **`ignore_senders`**        | `list[str]` | `[]`  | 忽略这些 QQ 号的撤回消息，不会触发转发。             |
| **`cache_expiration_time`** | `int`       | `300` | 消息缓存时间，单位: 秒。超过此时间的消息，撤回后将无法恢复。 |
| **`file_size_threshold_mb`** | `int`       | `300` | 视频和文件大小阈值，单位: MB。超过此大小的视频和文件将不会被缓存（设为 0 表示不限制）。 |
| **`forward_relay_group`** | `str` | `""` | 中转群号，用于转发合并消息。若配置，合并转发消息将先转发至此群以获取 ID，从而实现撤回监控。 |
| **`auto_recall_relay`** | `bool` | `True` | 是否自动撤回中转群的消息。建议开启以保持中转群整洁。 |

---

## 📅 版本更新

**v1.0**

* 新增支持合并转发消息的撤回监控（需配置中转群）。

**v0.3**

* 新增视频和文件大小阈值配置，超过阈值的文件将不会被缓存。

**v0.2**

* 新增支持艾特、系统表情、视频、文件、json(小程序)类型的消息。

**v0.1**

* 支持监控文本和图像消息。

---

## ❤️支持

* [AstrBot 帮助文档](https://astrbot.app)
* 如果您在使用中遇到问题，欢迎在本仓库提交 [Issue](https://github.com/Foolllll-J/astrbot_plugin_anti_revoke/issues)。


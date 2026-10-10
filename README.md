# 插件管家 🧩

> 查插件、装插件、开关与重载——聊天里的插件琐事，交给管家。

一个 `/pm` 入口，搞定 AstrBot 插件的日常管理。**所有命令仅限管理员使用**，无需额外配置或第三方依赖。

## 🚀 快速上手

- **版本**：AstrBot `>=4.27,<5`，已在 `4.27.4` 验证；其他版本请先测试。
- **安装**：放入实际运行目录的 `data/plugins/astrbot_plugin_manager`。
- **开用**：重载插件或重启 AstrBot，发送 `/pm` 查看帮助。示例使用 `/` 前缀，以实际配置为准。

## 📋 命令速查

| 命令 | 管家能做什么 |
| --- | --- |
| `/pm help` | 查看帮助 |
| `/pm list` | 查看插件列表 |
| `/pm info <插件名>` | 查看插件信息、指令、别名及停用状态 |
| `/pm reload <插件名>` | 重载插件 |
| `/pm enable <插件名>` | 启用插件 |
| `/pm disable <插件名>` | 停用插件 |
| `/pm add <仓库地址>` | 安装插件，保留正常版本检查 |
| `/pm remove <插件名> [--all] | 卸载插件，需再次确认               |

插件名也可以填写**目录名**，但必须精确匹配；不做模糊匹配、批量操作或旧命令别名。除卸载外，管理员群聊、私聊都能用。

改完插件代码？来一句：

```text
/pm reload astrbot_plugin_example
```

只重载这个插件，不全量重载、不重启 AstrBot。停用插件请先 `enable`，加载失败的请到 WebUI 恢复；共享依赖变更建议重启 AstrBot。

## 🗑️ 卸载，先确认再动手

在**管理员私聊**中申请，并在 **60 秒内**追加 `confirm` 确认。默认删除插件文件，保留配置与数据：

```text
/pm remove astrbot_plugin_example
/pm remove astrbot_plugin_example confirm
```

想连配置、文件数据和 KV 数据一起清理？**两次都带上 `--all`**：

```text
/pm remove astrbot_plugin_example --all
/pm remove astrbot_plugin_example --all confirm
```

确认必须来自同一管理员、同一平台私聊，目标和删除范围也要一致。超时、管家重载或 AstrBot 重启后，需要重新申请。

## 💡 使用边界

- 管家不能停用或卸载自己，AstrBot 保留插件也不能卸载。
- 操作失败可能已产生部分变更，不会自动回滚；请到 WebUI 核对或恢复，避免反复重试。

## 开发验证

在包含相邻 `AstrBot` 源码目录的工作区运行；源码在其他位置时设置 `ASTRBOT_SOURCE`：

```powershell
.\AstrBot\.venv\Scripts\python.exe -m pytest astrbot_plugin_manager\tests -q
.\AstrBot\.venv\Scripts\python.exe -m ruff check astrbot_plugin_manager
```

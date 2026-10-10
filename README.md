# 插件管家 🧩

> 查插件、看指令、找工具，安装与开关也顺手——聊天里的插件琐事，交给管家。

一个 `/pm` 入口，搞定 AstrBot 插件的日常管理。**所有命令仅限管理员使用**，默认配置即可开用，无需额外第三方依赖。

## 🚀 快速上手

- **版本**：AstrBot `>=4.27,<5`，已在 `4.27.4` 验证；其他版本请先测试。
- **安装**：放入实际运行目录的 `data/plugins/astrbot_plugin_manager`。
- **开用**：重载插件或重启 AstrBot，发送 `/pm` 查看帮助。示例使用 `/` 前缀，以实际配置为准。

## 📋 命令速查

| 命令 | 管家能做什么 |
| --- | --- |
| `/pm help` | 查看帮助 |
| `/pm list` | 查看插件列表 |
| `/pm info <插件名>` | 查看插件信息 |
| `/pm reload <插件名>` | 重载插件 |
| `/pm enable <插件名>` | 启用插件 |
| `/pm disable <插件名>` | 停用插件 |
| `/pm add <仓库地址>` | 安装插件 |
| `/pm remove <插件名> [--all]` | 卸载插件 |
| `/pm cmds [页码]` | 查看全部命令 |
| `/pm tools [页码]` | 查看模型工具 |

插件名也可以填写**目录名**，默认允许省略 `astrbot_plugin_` 前缀，剩余名称仍须精确匹配。不做模糊匹配、批量操作或旧命令别名。除卸载外，管理员群聊、私聊都能用。

## 🔎 指令与工具，一眼看清

`list` 一次列出全部插件；`cmds`、`tools` 每页 20 项，默认第一页，结果会附下一页命令：

```text
/pm cmds 2
/pm tools
/pm info dev_helper
```

插件列表有显示名称时显示 `显示名称（插件名）`；详情分别列出显示名称和插件名。未设置 `display_name` 时，列表只显示插件名，详情标注「显示名称：未声明」。加载失败插件按已有元数据展示显示名称，仍以目录名作为操作标识。显示名称只用于展示，不参与命令匹配。

工具目录包含插件工具、AstrBot 内置工具、已注册 MCP 工具，以及 Agent 入口和嵌套工具。同一工具对象只显示一次，不同对象即使同名也分别展示。`info` 只展示目标插件真正拥有的条目，不把它借用的其他插件或 MCP 工具算进去；参数只列名称，完整结构看 `tools`。

## ⚙️ 少打一点插件名

在 WebUI 的插件配置中切换 **允许省略插件名前缀**（`allow_prefix_omission`，默认开启）：

- 开启：`/pm reload example` 可操作 `astrbot_plugin_example`。
- 关闭：只接受原始插件全名或完整目录名。

对 `info`、`enable`、`disable`、`reload`、`remove` 的名称参数统一生效。短名若匹配不同插件，直接报歧义，不自动挑选目标；卸载仍需私聊确认。

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

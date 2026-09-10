# EPLAN AI 自动化工具包

[English](README.md) · **中文** · [Русский](README.ru.md)

[![MCP Badge](https://lobehub.com/badge/mcp/covagashi-eplan_2026_ia_mcp_scripts)](https://lobehub.com/mcp/covagashi-eplan_2026_ia_mcp_scripts)

面向 **EPLAN Electric P8** 与 **EPLAN EEC Pro 2026** 的 AI 辅助自动化工具，基于模型上下文协议（MCP）构建。

本仓库包含四个相互独立的子项目：一个直接驱动正在运行的 EPLAN 实例的本地 MCP 服务器，以及三个部署在 Cloudflare Workers 上、提供 EPLAN 文档检索的远程 MCP 服务器。

> 正在和大语言模型一起使用本仓库？请阅读 [`llm.md`](llm.md) —— 它以面向 LLM 的方式
> 说明了本工具包能做什么、可配置哪些内容。

## 仓库结构

```
.
├── eplan-p8-mcp-server/          # 本地：控制 EPLAN P8 的 MCP 服务器
├── cloudflare-rag-eplan-p8/      # 远程：通过 MCP 提供 P8 文档 RAG 的 Cloudflare Worker
├── cloudflare-rag-eecpro/        # 远程：通过 MCP 提供 EEC Pro 文档 RAG 的 Cloudflare Worker
├── cloudflare-rag-eplan-2027/    # 远程：通过 MCP 提供 2027 API 维基的 Cloudflare Worker（D1/FTS5 关键词检索）
└── claude-skills/                # 技能：covagashi/eplan-development-skill 的镜像
```

| 目录 | 类型 | 用途 | 适用的 EPLAN 产品 |
|---|---|---|---|
| `eplan-p8-mcp-server/` | 本地 Python MCP | 从 Claude 驱动正在运行的 EPLAN 实例：打开/关闭项目、导出、报表、脚本 | EPLAN Electric P8 |
| `cloudflare-rag-eplan-p8/` | 远程 Cloudflare Worker | 以远程 MCP + REST API 的形式提供 P8 文档索引 | EPLAN Electric P8 |
| `cloudflare-rag-eecpro/` | 远程 Cloudflare Worker | 以远程 MCP + REST API 的形式提供 EEC Pro 文档索引 | EPLAN EEC Pro 2026 |
| `cloudflare-rag-eplan-2027/` | 远程 Cloudflare Worker | 以远程 MCP 的形式提供 2027 API 维基；使用 D1 + FTS5 关键词检索，与上面的语义索引互补 | EPLAN Electric P8 2027 |
| `claude-skills/eplan-development/` | Claude Code Skill | 教 Claude 写出正确的 EPLAN 脚本、API 代码和 Remote Client 应用；独立仓库 [**eplan-development-skill**](https://github.com/covagashi/eplan-development-skill) 的镜像 | EPLAN Electric P8 |

每个子项目都有各自的 README，其中包含安装和使用的详细说明。

## 什么是 MCP？

**MCP（Model Context Protocol，模型上下文协议）** 是一项开放标准，它让 AI 助手能够与外部工具和服务交互。Claude 不再只是生成代码，而是可以实时在 EPLAN 中真正*执行*操作，并通过检索查阅文档。

## 快速开始

### 本地 EPLAN 自动化（P8）

```bash
pip install pythonnet mcp
claude mcp add eplan -- python YOURPATH/eplan-p8-mcp-server/mcp_server/server.py
claude mcp list   # 应当能列出 "eplan"
```

随后启动 EPLAN，打开 Claude Code，并输入 `connect to eplan`。完整指南见
[`eplan-p8-mcp-server/mcp_server/README.md`](eplan-p8-mcp-server/mcp_server/README.md)。

![Claude CLI configured](image.png)

#### 使用前提

服务器连接之前，必须先在 EPLAN 中启用远程控制：在 **文件 → 设置… → 工作站 → 接口 → 远程访问** 中打开 **允许通过 Remote Client 进行远程访问**。

![Allow remote access via Remote Client](Remoting_Setting_AllowLocalAccess.png)

#### 服务器提供哪些工具

在默认的 `full` 模式下，服务器共发布 **199 个工具**：

| 分组 | 数量 | 覆盖内容 |
|---|---|---|
| 连接 / 辅助 | 8 | 连接、版本选择、状态查询、扩展模块列表 |
| 类型化 EPLAN 操作（`eplan_*`） | 183 | 每个已记录或经过验证的操作对应一个工具，全部在 QuietMode 下的 C# 脚本中静默执行，因此不会有任何 EPLAN 对话框阻塞无人值守的运行 |
| 操作目录（`eplan_action_catalog` / `_describe` / `_run` / `_ribbon_catalog`） | 4 | 触达另外约 1,050 个仅以界面按钮形式存在的操作，这些操作来自安装目录中的 `MFTools.xml`，而非官方文档 |
| 资产管理壳（`aas_*`） | 4 | AAS/AASX 数字孪生的导出与导入 |

那约 1,050 个目录操作**刻意**没有各自封装成一个工具：那样会让工具总数翻三倍，并且拖累其他所有工具的选择准确度。

在这 183 个类型化工具中，有四个实时 DataModel 工具 ——
`eplan_live_query_functions`、`eplan_live_query_pages`、`eplan_live_set_function_text`
和 `eplan_live_set_connection_designations` —— 它们通过运行时反射读取和编辑当前打开项目的对象模型，绕开脚本引擎对静态 `using` 指令的限制。

除了单个操作之外，这套工具还覆盖了实现全自动「开发 → 部署 → 测试」循环所需的基础能力：

- **应用生命周期** —— `eplan_app_launch` / `eplan_app_shutdown` / `eplan_app_restart`：退出 EPLAN、替换插件 DLL、重新启动、重新连接、重新打开项目。
- **一次性测试夹具** —— `eplan_scratch_project_*`：从模板克隆出的 scratch 项目。
- **诊断** —— `eplan_get_system_messages`：读取 EPLAN 的消息树，看到与用户在界面上看到的完全相同的错误与警告。
- **私有扩展模块** —— 见[下文](#私有扩展模块eplan_mcp_extensions)。

逐个工具的完整清单见 [项目 Wiki](https://github.com/covagashi/eplan-rag-mcp/wiki)。

#### Discovery 模式

`EPLAN_MCP_MODE=discovery` 只发布 13 个工具而非 199 个，代价是每个任务多一次检索往返，换来一份不那么消耗 token 的工具列表。对于每次请求都发送全部工具完整 schema 的 MCP 客户端来说，这很划算。

**但在 Claude Code 中请不要启用它。** Claude Code 本身已经会延迟加载工具 schema —— 先给出名称列表，按需再取 schema —— 因此 `full` 模式下的 199 个工具，其成本已经和 discovery 的 13 个差不多；再叠加 discovery 的「搜索 → 描述 → 调用」这层间接，只是白白多一次往返。权衡与实测数据见
[`eplan-p8-mcp-server/mcp_server/README.md`](eplan-p8-mcp-server/mcp_server/README.md#discovery-mode-eplan_mcp_mode)。

### 远程文档 RAG（P8、EEC Pro、2027）

这些服务已经部署完毕、开箱即用 —— 无需任何本地数据：

```bash
# EPLAN Electric P8 文档（2026，语义搜索）
claude mcp add eplan-rag -- cmd /c npx mcp-remote https://rag2026.covaga.xyz/mcp

# EPLAN EEC Pro 2026 文档
claude mcp add eecpro-rag -- cmd /c npx mcp-remote https://rageecpro.covaga.xyz/mcp

# EPLAN Electric P8 文档（2027，关键词/全文搜索）
claude mcp add eplan-wiki-2027 -- cmd /c npx mcp-remote https://rag2027.covaga.xyz/mcp
```

`eplan-wiki-2027` 是一个独立的服务，而不是 `eplan-rag` 从 2026 到 2027 的升级版：它索引的文档版本不同，*并且*检索方式也不同（对 [`cloudflare-rag-eplan-2027/`](cloudflare-rag-eplan-2027/) 内置维基使用 SQLite FTS5/bm25 关键词匹配，而非 Vectorize + bge 语义搜索）。在真实问题上做过正面对比，两者的失效方式不同：精确名称查询（「X 的方法签名是什么」）FTS5 更准，而当提问完全不含文档原词时语义搜索更强。建议两个都装。

两者同时提供普通的 REST API，在开发过程中用于核对 EPLAN 操作名称和参数非常方便：

```bash
curl -X POST https://rag2026.covaga.xyz/search -H "Content-Type: application/json" \
     -d '{"query": "export project pdf", "topK": 3}'

curl -X POST https://rag2027.covaga.xyz/search -H "Content-Type: application/json" \
     -d '{"query": "FindAction", "topK": 3}'
```

工具、REST 接口和架构说明见 [`cloudflare-rag-eplan-p8/README.md`](cloudflare-rag-eplan-p8/README.md) 与 [`cloudflare-rag-eecpro/README.md`](cloudflare-rag-eecpro/README.md)。

### 用于 EPLAN 开发的 Claude Code Skill

MCP 服务器让 Claude 能够对 EPLAN *执行操作*；而 [**eplan-development**](https://github.com/covagashi/eplan-development-skill) 这个 Skill 则教会它 *写出正确的 EPLAN 代码*：脚本入口点、经过验证的操作参数、部件数据库访问、Remote Client 自动化（动态端口、无界面 EPLAN、Cogineer），以及生产环境中的各种陷阱 —— 伪异步命令阻塞、消息循环监视线程、dispose 规范、EPLAN 2025 remoting 的变化。

该 Skill 拥有自己的独立仓库，并且刻意做到**与宿主无关**：它不假定任何 MCP 服务器、任何特定的脚本执行器或任何特定的文档索引，因此无论你是否使用本仓库的其他部分，它都可以单独使用。该仓库为权威副本，本仓库的 `claude-skills/` 只是其镜像。

```
/plugin marketplace add covagashi/eplan-development-skill
/plugin install eplan-development@eplan-skills
```

本仓库同时也是一个插件市场，因此也可以直接从这里安装该 Skill：

```
/plugin marketplace add covagashi/eplan-rag-mcp
/plugin install eplan-development@eplan-tools
```

手动安装方式和更多细节见 [`claude-skills/eplan-development/README.md`](claude-skills/eplan-development/README.md)。

## 添加新的 EPLAN 操作

本地 MCP 服务器会根据每个 actions 包的 `__all__` 列表**动态**注册工具，因此新增一个操作只需两步，无需为每个工具编写样板代码。

### 1. 实现该操作

在 `eplan-p8-mcp-server/mcp_server/api/actions/<your_module>.py` 中：

```python
def open_project(project_path: str, open_mode: str = None) -> dict:
    """Open a project in EPLAN.

    Args:
        project_path: Full path to the .elk project file.
        open_mode: "Standard", "ReadOnly", or "Exclusive" (optional).
    """
    manager, error = _get_connected_manager()
    if error:
        return error
    action = _build_action("ProjectOpen", Project=project_path, OpenMode=open_mode)
    return manager.execute_action(action)
```

### 2. 导出该函数

在 `eplan-p8-mcp-server/mcp_server/api/actions/__init__.py` 中，把该函数加入 imports **以及** `__all__`。它随后会被自动注册为 `eplan_open_project`。

### 3. 重启 MCP 服务器

重启 Claude / 服务器之后，新工具即可使用。

### 4. 对照官方文档进行校验（可选）

`eplan-p8-mcp-server/tools/validate_actions.py` 会把封装函数中声明的每一个操作名称和参数，与官方 EPLAN 文档 RAG 进行交叉核对，并输出一份 markdown 报告：

```bash
python eplan-p8-mcp-server/tools/validate_actions.py
```

![EPLAN test](image-1.png)

### 小贴士

1. **对照文档核实。** 使用远程 P8 RAG（`https://rag2026.covaga.xyz`）确认 EPLAN 操作的准确名称和参数。
2. **写好文档字符串和类型注解。** 它们会成为 LLM 所看到并依赖的工具描述和输入 schema。
3. **谨慎处理路径。** Windows 路径需要转义（`\\`）或改用正斜杠（`/`）。

## 私有扩展模块（`EPLAN_MCP_EXTENSIONS`）

服务器可以**从本仓库之外加载额外的工具模块** —— 用于公司专有或私有的工具（自定义插件测试框架、内部工作流），这些内容不适合放在公开仓库中。

把 `EPLAN_MCP_EXTENSIONS` 环境变量指向一个或多个目录（Windows 上用 `;` 分隔）。其中所有不以 `_` 开头的顶层 `*.py` 文件都会在启动时被导入，其 `__all__` 中的函数会像内置操作一样被注册为 MCP 工具：

```python
# my_company_tools.py  （位于私有仓库，而非 eplan-rag-mcp 中）
TOOL_PREFIX = "acme_"          # 可选，默认为 "eplan_"
__all__ = ["run_smoke_test"]

import actions                  # 服务器的 api/ 目录已在 sys.path 中
from actions._base import _get_connected_manager

def run_smoke_test(project_path: str) -> dict:
    """Docstring becomes the tool description the LLM sees."""
    clone = actions.scratch_project_create(project_path)
    ...
    return {"success": True}
```

规则与行为：

- `TOOL_PREFIX` 为工具加上命名空间（上例中即 `acme_run_smoke_test`）。
- 扩展模块可以导入内置操作所使用的一切：`actions`、`actions._base`、`actions.scripted._execute_script`（在 EPLAN 内运行 C#）、`eplan_connection`。
- 出错的扩展会在 stderr 上报告并被跳过，绝不会导致服务器无法启动。
- `eplan_list_extensions` 会显示已加载的内容。

结合生命周期工具与 scratch 项目夹具，这就构成了一个用于开发私有 EPLAN 插件的全自动循环：编译 DLL → 部署 → `eplan_app_restart` → 确认插件的操作已注册（例如通过 `FindAction` 脚本）→ 在一次性 scratch 项目上运行它们 → 用 `eplan_get_system_messages` 捕获 EPLAN 提出的任何抱怨。

## EPLAN 版本选择（自动）

**无需任何配置**。服务器启动时会扫描 `C:\Program Files\EPLAN\Platform` 查找已安装的版本，然后：

- **自动模式（默认）：** `eplan_connect` 会连接**已安装的最新版本**，并自动选用相应的 .NET 运行时 —— EPLAN 2027 及以上使用 coreclr，2026 及更早版本使用 .NET Framework。
- **显式模式：** LLM 可以调用 `eplan_versions` 列出已安装的版本，再用 `eplan_connect(version="2026")` 连接到指定版本 —— 例如「connect to eplan 2026」。

注意事项：

- EPLAN 安装在非标准路径？把 `EPLAN_PLATFORM_ROOT` 环境变量设为其 `Platform` 目录即可。
- 一旦某个版本的 DLL 被加载进进程，切换到另一个版本就需要重启 MCP 服务器 —— .NET 运行时无法在运行期间更换。
- `eplan_connect` 也接受 `host`（以及 `"host:port"`），用于连接另一台机器上的 EPLAN 实例；端口自动检测仅在 localhost 上有效。

## 相关仓库

| 仓库 | 内容 |
|---|---|
| [**eplan-development-skill**](https://github.com/covagashi/eplan-development-skill) | 上文那个 Claude Code Skill 的独立、与宿主无关的版本。用 `/plugin marketplace add covagashi/eplan-development-skill` 安装。 |
| [**eplan-ctxmenu-kit**](https://github.com/covagashi/eplan-ctxmenu-kit) | 向 EPLAN 的右键菜单添加自己的条目，并读取用户点击的那一行。包含一个探测工具和一个完整示例。 |

## 相关资源

- [EPLAN API 文档](https://www.eplan.help/)
- [MCP 协议规范](https://modelcontextprotocol.io/)
- [Claude Code 文档](https://docs.anthropic.com/claude-code)

## 许可证

MIT —— 见 [`license`](license)。

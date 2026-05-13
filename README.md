# Hermes Agent

> 基于 Nous Research 的 Hermes 深度魔改版 — 自我进化的 AI 代理，支持多平台网关、智能路由、动态工具分层、技能系统、MCP 协议

## 概览

Hermes 是一个全功能的 AI 代理系统，在 Nous Research 原版基础上进行了大量定制开发。核心思路是：**一个代理，所有平台，按需分配能力**。

```
hermes                    # CLI 聊天
hermes-agent              # 直接运行
hermes-acp                # ACP 协议适配器
hermes gateway start      # 启动多平台网关
```

---

## 魔改功能全览

以下是相对于原版 Hermes 的所有定制和增强功能：

### 1. 智能模型路由 (Smart Model Routing)

自动根据消息复杂度选择不同模型/工具集，大幅节省 token。

```
simple  (简单问题)  → light  模式：无工具，用 cheap_model
general (日常对话)  → medium 模式：仅 core + meta 工具 (~60% token 节省)
complex (复杂任务)  → heavy  模式：全部工具 + MCP + 技能
```

- 复杂度基于：关键词、消息长度、代码块、URL、约束链、超时恢复等信号
- 支持中英文关键词（"代码""修复""重构""排查""debug""patch"等）
- 可配置 `force_light_contains` / `force_heavy_contains` 强制路由
- 所有阈值可调：`max_simple_chars` / `min_complex_words` 等

### 2. 三层动态工具分级 (Tool Tiers)

不再一把梭全部工具。按复杂度动态分配：

| 层级 | 包含工具 | 估算 Token |
|------|---------|-----------|
| **Core** | web_search, web_extract, terminal, process, read_file, write_file, patch, search_files | ~1,500 |
| **Meta** | skills_list, skill_view, todo, memory, session_search, clarify | ~1,300 |
| **Heavy** | browser_*, vision_*, image_generate, tts, execute_code, delegate_task, cronjob, send_message, ha_*, skill_manage | ~1,600 |

general 轮从 ~4,400 tokens 降到 ~2,800，**节省约 37%**。

### 3. 多平台消息网关 (Multi-Platform Gateway)

一个网关同时服务所有平台，共享会话管理、记忆、技能系统：

| 平台 | 状态 | 特性 |
|------|------|------|
| CLI | ✅ | 完整终端体验，皮肤系统，实时流式 |
| Telegram | ✅ | 命令、群组、内联键盘、语音消息、流式编辑 |
| Discord | ✅ | 频道、私信、Slash 命令、流式 |
| WhatsApp | ✅ | 消息收发 |
| Slack | ✅ | Bolt 框架、Socket Mode |
| Signal | ✅ | 端到端加密消息 |
| Home Assistant | ✅ | 智能家居控制面板 |
| Matrix | ✅ | 端到端加密、多房间 |
| DingTalk | ✅ | 钉钉机器人 |
| Feishu/Lark | ✅ | 飞书机器人 |
| TUI Gateway | ✅ | 终端 UI 网关 |
| Web Dashboard | ✅ | CPA 管理 WebUI |

### 4. 心跳调度器 (Heartbeat Scheduler)

网关空闲时，代理可以主动发起心跳轮次：

- 定期执行轻量级代理轮次（在 main session 上下文中）
- `HEARTBEAT_OK` 静默确认机制，不干扰用户
- 可配置 TTL 和间隔
- 用于：背景知识更新、主动建议、定时检查

### 5. CLIProxyAPI (CPA) 统一代理层

所有请求统一通过 CPA 路由，不再直连上游 Provider：

```
Hermes → CPA (/v1) → 上游 Provider (OpenRouter / Anthropic / OpenAI / 本地模型)
```

- CPA WebUI 管理上游 Provider、OAuth 账户、路由策略、故障转移
- OpenAI 兼容接口 (`/v1`) + Anthropic 兼容接口 (`/anthropic`)
- 支持 API Key 鉴权、多账户池
- Hermes 只认 `provider: cliproxyapi`

### 6. 终端环境矩阵 (6 种运行后端)

不只本地执行：

| 后端 | 场景 |
|------|------|
| **local** | 本地直接执行 |
| **ssh** | 远程服务器，代理代码本地运行 |
| **docker** | Docker 容器隔离 |
| **singularity** | HPC 集群、共享计算环境 |
| **modal** | Modal 云端 GPU/Serverless |
| **daytona** | Daytona 云端沙箱，持久化工作区 |

所有后端共享 sudo 密码管道、超时控制、资源限制配置。

### 7. MCP 协议完整实现 (Model Context Protocol)

接入 MCP 生态的任意工具服务器：

- **Stdio 服务器**: 通过子进程启动 MCP 服务器
- **HTTP/SSE 服务器**: 连接远程 MCP 端点
- **OAuth 2.1 授权**: 内置 OAuth 客户端，支持需要认证的 MCP 服务器
- **Sampling 支持**: 服务器发起的 LLM 请求，可配置模型/速率/审计
- 自动发现并注册 MCP 工具到工具注册表

### 8. 自进化技能系统 (Self-Improving Skills)

代理可以创建、改进、分享技能：

- 完成复杂任务后自动提示创建技能
- 技能存储在 `~/.hermes/skills/`
- **Skills Hub**: 从 GitHub 仓库搜索/安装/管理技能
- **外部技能目录**: 跨代理共享只读技能
- 技能保护机制：访问控制和权限管理
- 每 N 轮工具调用提示创建技能

### 9. 上下文压缩引擎 (Context Compression)

对话太长时自动压缩中间轮次：

- 跟踪 API 返回的实际 token 用量（非估算）
- 达到阈值（默认 50%）时触发压缩
- 保护前 3 轮 + 最近 N 条消息（默认 20 条）
- 用快速模型总结中间轮次
- 压缩后无缝继续对话
- 所有参数可调：threshold / target_ratio / protect_last_n

### 10. 持久化记忆系统 (Persistent Memory)

跨会话记忆，两种存储：

| 存储 | 用途 | 字符限制 |
|------|------|---------|
| **MEMORY.md** | 代理个人笔记：环境事实、约定、学到的知识 | 2,200 chars (~800 tokens) |
| **USER.md** | 用户画像：偏好、沟通风格、期望 | 1,375 chars (~500 tokens) |

- 代理自主管理裁剪（达到限制时合并或替换）
- 记忆提醒：每 N 轮提示代理保存记忆
- 记忆冲刷：压缩/重置/退出前给代理一轮保存记忆
- **Honcho 集成**: AI-native 跨会话用户建模（可选）

### 11. 睡眠模式 (Sleep Mode)

对话间隙自动进行后台学习和维护：

| 功能 | 说明 |
|------|------|
| 记忆审查 | 后台审查并改进记忆 |
| 技能审查 | 后台审查并改进技能/工作流 |
| 外部记忆同步 | 同步已完成轮次到外部记忆提供者 |
| L4 归档 | 归档空闲会话到 L4 记忆 |
| L4 压缩 | 裁剪旧的低优先级 L4 行 |

配置档位：`off` / `light` / `balanced` / `deep`

### 12. 浏览器自动化 (Browser Automation)

基于 Browserbase 的云端浏览器：

- navigate / snapshot / click / type / scroll / back / press
- get_images / vision（截图视觉分析）
- 不活跃超时自动关闭（默认 2 分钟）
- 支持 Firecrawl 页面抓取

### 13. 视觉与多媒体工具

| 工具 | 能力 | 依赖 |
|------|------|------|
| vision_analyze | 图像分析、OCR | 辅助模型 |
| image_generate | FLUX 图像生成 | FAL_KEY |
| text_to_speech | 语音合成 | Edge TTS（免费）或 ElevenLabs/OpenAI/MiniMax/Mistral |
| transcription (STT) | 语音转文字 | faster-whisper（本地免费）或 Groq/OpenAI/Mistral API |
| voice_mode | 语音对话模式 | 同上 |

### 14. 定时任务系统 (Cron Jobs)

在 CLI 中管理定时任务：

- 创建/列表/更新/暂停/恢复/运行/删除
- 支持 cron 表达式
- 任务执行在独立上下文中
- 共享记忆和技能系统

### 15. 子代理委派 (Subagent Delegation)

spawn 独立子代理处理子任务：

- 隔离上下文，不污染主会话
- 支持单任务和批量模式（最多 3 个并行）
- 可配置子代理的工具集和模型
- 结果压缩后返回主代理

### 16. 代码执行沙箱 (Code Execution Sandbox)

Python 脚本通过 RPC 调用 Hermes 工具：

- 中间结果不进入 LLM 上下文窗口
- 可配置超时（默认 300s）和最大工具调用数（默认 50）
- 适合数据处理、批量操作

### 17. 会话管理系统

| 特性 | 说明 |
|------|------|
| 自动重置策略 | both / idle / daily / none |
| 不活跃超时 | 可配置分钟数（默认 24h） |
| 每日定时重置 | 指定小时（默认 4 AM） |
| 按用户隔离 | 群组聊天中每人独立会话（默认开启） |
| 流式输出 | 实时编辑消息，支持 Telegram/Discord/Slack |
| 会话日志 | 自动保存完整轨迹到 logs/ |

### 18. CLI 皮肤系统 (Skin System)

自定义 CLI 外观：

- 内置皮肤：`default`（金色） / `ares`（红铜） / `mono`（灰度） / `slate`（蓝灰）
- 自定义皮肤：YAML 文件定义颜色、动画、文字
- 运行时 `/skin <name>` 切换
- 可配置：横幅颜色、状态动画、工具前缀、响应框标题

### 19. 安全与审计

| 特性 | 说明 |
|------|------|
| Tirith 预执行扫描 | 检测 homograph URL、pipe-to-shell、注入、env 操纵 |
| CPA 边界 (CPA Boundary) | 访问控制策略和安全边界 |
| PII 脱敏 | 可选的电话号码/ID 哈希脱敏 |
| 命令注入防护 | 所有 `shell=True` 已替换为参数数组形式 |
| 异常审计日志 | 所有 `except` 块记录 `exc_info=True` |

### 20. 其他特色

| 特性 | 说明 |
|------|------|
| **模型别名** | `/model fast` 快速切换预配置模型 |
| **推理力度控制** | xhigh / high / medium / low / none |
| **人格系统** | 6 种预设人格 + `/personality` 自定义 |
| **Human Delay** | 消息间添加类人延迟 |
| **背景进程通知** | 终端后台任务完成通知到消息平台 |
| **完成响铃** | 代理完成时终端响铃 |
| **工作区隔离** | Git worktree 隔离，多代理并行工作 |
| **轨迹压缩** | 对话历史优化和压缩 |
| **模型回退链** | 主模型失败时自动回退到备用模型 |

---

## 快速开始

### 安装

```bash
pip install hermes-agent

# 或完整安装（含消息平台、语音等）
pip install "hermes-agent[all]"
```

### 配置

```bash
# 复制配置模板
cp cli-config.yaml.example cli-config.yaml

# 编辑 .env 文件
cat > ~/.hermes/.env << EOF
CLIPROXY_API_KEY=your-key
EOF
```

### 启动

```bash
# CLI 聊天
hermes

# 启动多平台网关
hermes gateway start

# 启动 WebUI 面板
hermes dashboard start

# 运行诊断
hermes doctor
```

---

## 配置参考

完整配置见 [cli-config.yaml.example](cli-config.yaml.example)，包含所有可选项的注释说明。

### 核心配置项

```yaml
model:
  default: "gpt-5(8192)"
  provider: "cliproxyapi"

smart_model_routing:
  enabled: true
  route_modes:
    simple: light
    general: medium
    complex: heavy

terminal:
  backend: "local"    # local | ssh | docker | singularity | modal | daytona
  cwd: "."
  timeout: 180

memory:
  memory_enabled: true
  user_profile_enabled: true

compression:
  enabled: true
  threshold: 0.50

sleep_mode:
  enabled: true
  profile: balanced
```

---

## 架构

```
┌──────────────────────────────────────────────────────────┐
│                       用户入口                            │
│  CLI │ Telegram │ Discord │ WhatsApp │ Slack │ ...       │
└────────────────────────┬─────────────────────────────────┘
                         ▼
┌──────────────────────────────────────────────────────────┐
│                    Hermes 网关                            │
│  会话管理 │ 平台适配 │ 流式传输 │ 心跳调度 │ 会话重置     │
└────────────────────────┬─────────────────────────────────┘
                         ▼
┌──────────────────────────────────────────────────────────┐
│                AI Agent 核心                              │
│  Smart Routing │ Tool Tiers │ Skills │ Memory │ MCP      │
└────────────────────────┬─────────────────────────────────┘
                         ▼
┌──────────────────────────────────────────────────────────┐
│                CPA 代理层                                 │
│    OpenAI 兼容 /v1 │ Anthropic 兼容 /anthropic            │
│    上游路由 │ 故障转移 │ 多账户池 │ OAuth                 │
└────────────────────────┬─────────────────────────────────┘
                         ▼
┌──────────┬──────────┬──────────┬──────────┬──────────────┐
│OpenRouter│ Anthropic│  OpenAI  │   本地   │ 更多 Provider │
└──────────┴──────────┴──────────┴──────────┴──────────────┘
```

---

## 项目结构

```
hermes-agent/
├── agent/                  # AI 代理核心
│   ├── smart_model_routing.py   # 智能路由
│   ├── agent_init.py            # 代理初始化（从 run_agent.py 抽取）
│   ├── auxiliary_client.py      # 辅助模型客户端
│   ├── credential_pool.py       # 凭证池
│   └── anthropic_adapter.py     # Anthropic 适配
├── gateway/                # 多平台网关
│   ├── run.py                   # 网关主循环
│   ├── heartbeat.py             # 心跳调度器
│   ├── message_handler.py       # 消息路由（从 run.py 抽取）
│   ├── config.py                # 网关配置
│   ├── session.py               # 会话管理
│   ├── status.py                # 状态管理
│   └── platforms/               # 平台适配器
│       ├── base.py              # 平台基类
│       ├── telegram.py
│       ├── discord.py
│       ├── whatsapp.py
│       └── ...
├── tools/                  # 工具系统
│   ├── registry.py              # 工具注册中心
│   ├── mcp_tool.py              # MCP 协议客户端
│   ├── mcp_oauth.py             # MCP OAuth 2.1
│   ├── terminal_tool.py         # 终端执行
│   ├── file_operations.py       # 文件操作
│   ├── web_tools.py             # 网页搜索/抓取
│   ├── vision_tools.py          # 视觉分析
│   ├── image_generation_tool.py # 图像生成
│   ├── memory_tool.py           # 持久化记忆
│   ├── skills_*.py              # 技能系统
│   ├── browser_providers/       # 浏览器自动化
│   └── environments/            # 运行环境
│       ├── local.py             # 本地
│       ├── ssh.py               # SSH 远程
│       ├── docker.py            # Docker 容器
│       ├── singularity.py       # Singularity
│       └── modal.py             # Modal 云端
├── hermes_cli/             # CLI 和运维
│   ├── main.py                  # CLI 主入口
│   ├── gateway.py               # 网关管理
│   ├── setup.py                 # 安装向导
│   ├── doctor.py                # 诊断工具
│   ├── auth.py                  # 认证管理
│   └── models.py                # 模型管理
├── cron/                   # 定时任务
├── tui_gateway/            # TUI 网关
├── acp_adapter/            # ACP 协议适配
├── toolsets.py             # 工具集定义和分层
├── model_tools.py          # 模型工具
├── trajectory_compressor.py# 轨迹压缩
├── utils.py                # 通用工具
├── cli.py                  # CLI 入口
└── cli-config.yaml.example # 配置模板
```

---

## 工具列表

| 工具 | 分类 | 说明 |
|------|------|------|
| web_search | core | 网页搜索（Tavily / Exa / Firecrawl） |
| web_extract | core | 网页内容抓取 |
| terminal | core | Shell 命令执行 |
| process | core | 进程管理 |
| read_file | core | 文件读取 |
| write_file | core | 文件写入 |
| patch | core | 代码补丁 |
| search_files | core | 文件搜索 |
| skills_list | meta | 技能列表 |
| skill_view | meta | 技能查看 |
| todo | meta | 待办事项 |
| memory | meta | 持久化记忆 |
| session_search | meta | 会话搜索 |
| clarify | meta | 需求澄清 |
| browser_* | heavy | 浏览器自动化 |
| vision_analyze | heavy | 图像分析 |
| image_generate | heavy | 图像生成 |
| text_to_speech | heavy | 语音合成 |
| execute_code | heavy | 代码执行沙箱 |
| delegate_task | heavy | 子代理委派 |
| cronjob | heavy | 定时任务管理 |
| send_message | heavy | 跨平台消息发送 |
| ha_* | heavy | Home Assistant |
| skill_manage | heavy | 技能管理 |
| skills_hub | heavy | 技能市场 |
| mixture_of_agents | heavy | 混合代理推理 |
| rl_* | heavy | RL 训练 |

---

## 环境变量

| 变量 | 用途 |
|------|------|
| `CLIPROXY_API_KEY` | CPA 认证 |
| `OPENROUTER_API_KEY` | OpenRouter 回退 |
| `ANTHROPIC_API_KEY` | Anthropic 直接 |
| `BROWSERBASE_API_KEY` | 浏览器自动化 |
| `FAL_KEY` | 图像生成 |
| `GROQ_API_KEY` | 语音转文字 |
| `FIRECRAWL_API_KEY` | 网页抓取 |
| `TAVILY_API_KEY` | 网页搜索 |
| `HONCHO_API_KEY` | 跨会话记忆 |

---

## 开发

```bash
# 安装开发依赖
pip install "hermes-agent[dev]"

# 运行测试
pytest tests/ -x --tb=short

# 运行特定测试
pytest tests/tools/test_registry.py -v
```

---

## License

MIT
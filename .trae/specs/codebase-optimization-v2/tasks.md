# Tasks

- [x] Task 1: 安全加固 — 修复 shell=True 和异常吞没
  - [x] 1.1 替换 `cli.py:6389`、`tui_gateway/server.py:4017,5671`、`tools/transcription_tools.py:505`、`tools/environments/docker.py:544,553` 中的 `shell=True` 为参数数组形式
  - [x] 1.2 为 `toolsets.py` 中 7 处 `except Exception: pass` 添加 `logger.debug("...", exc_info=True)`
  - [x] 1.3 为 `run_agent.py` 中 19 处 `except Exception: pass` 添加 `logger.debug("...", exc_info=True)`
  - [x] 1.4 检查 `send_message_tool.py` — 已有 exc_info=True
  - [x] 1.5 检查 `file_operations.py:102` — 已有 exc_info=True

- [x] Task 2: 代码清理 — print → logger + 修复损坏代码
  - [x] 2.1 将 `run_agent.py` 中 47 处 `print()` 迁移为 `logger.info/warning/debug/error`
  - [x] 2.2 `web_tools.py` 底部 print — 已在 `if __name__` 块内，无需修改
  - [x] 2.3 修复 `run_agent.py` 中 `_NEWLINE_AFTER_THINK_RE` 等损坏的正则表达式
  - [x] 2.4 检查未使用的 import — 均在使用，无需移除

- [x] Task 3: 架构拆分 — 拆分超大文件
  - [x] 3.1 创建 `agent/agent_init.py`（1450行），run_agent.py __init__ 改为调用 init_agent()
  - [x] 3.2 创建 `gateway/message_handler.py`（177行），run.py 减少 122 行
  - [x] 3.3 import 兼容性使用延迟导入

- [x] Task 4: 性能优化 — 补齐遗漏的正则预编译和 env 缓存
  - [x] 4.1 `gateway/run.py` 4 个预编译 + `cli.py` 1 个预编译
  - [x] 4.2 `terminal_tool.py` 缓存 `os.getenv("TERMINAL_CWD")`
  - [x] 4.3 agent cache 签名修复（已在之前完成）

- [x] Task 5: 测试覆盖 — 为缺失模块添加测试 + 回归测试
  - [x] 5.1 编写 `tests/tools/test_registry.py`（85 个测试，全部通过）
  - [x] 5.2 回归测试通过（34 passed + 85 new = 119 全部通过）

# Task Dependencies

- Task 2 依赖 Task 1（先修安全再修代码风格）
- Task 3 独立于 Task 1/2，可并行
- Task 4 独立，可并行
- Task 5 依赖 Task 1/2/3/4 完成后运行验证
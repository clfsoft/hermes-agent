# Checklist

- [x] **1.1 shell=True 全部替换** — cli.py、tui_gateway/server.py、transcription_tools.py、environments/docker.py 中无残留 `shell=True`
- [x] **1.2 except Exception 日志覆盖** — toolsets.py、run_agent.py、send_message_tool.py、file_operations.py 中所有 bare except 已添加 `logger.debug(exc_info=True)`（或已有）
- [x] **2.1 print → logger** — run_agent.py 中 47 处 `print()` 已迁移为 `logger.info/warning/debug/error`
- [x] **2.2 web_tools print** — 已在 `if __name__ == "__main__"` 块内
- [x] **2.3 损坏正则修复** — run_agent.py 中 `_NEWLINE_AFTER_THINK_RE` 等正则表达式正确且可编译
- [x] **3.1 run_agent.py 拆分** — `agent/agent_init.py` 存在且导入正常（1450 行），run_agent.py 减少 ~300 行
- [x] **3.2 gateway/run.py 拆分** — `gateway/message_handler.py` 存在且导入正常（177 行），run.py 减少 122 行
- [x] **4.1 正则预编译** — gateway/run.py（4 个）+ cli.py（1 个）已提升为模块级常量
- [x] **4.2 env 缓存** — terminal_tool.py 中 `os.getenv("TERMINAL_CWD")` 已缓存
- [x] **5.1 registry 测试** — `tests/tools/test_registry.py` 85 个测试全部通过
- [x] **回归测试** — 34 个已有测试 + 85 个新测试 = 119 全部通过
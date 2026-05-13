# 全量代码审查优化 Spec

## Why

代码库经过多轮迭代后积累了安全风险、性能瓶颈和架构臃肿问题：`shell=True` 子进程调用存在命令注入风险，大量 `except Exception: pass` 吞没异常，`print()` 混入生产代码，4 个核心文件均超过 10000 行难以维护。本轮审查旨在系统性识别并修复这些问题，提升代码质量和可维护性。

## What Changes

### 安全加固
- 移除所有 `shell=True` 子进程调用，改用参数数组形式
- 为所有 `except Exception: pass` 添加 `logger.debug()` 日志（含 `exc_info=True`）
- 检查 subprocess 调用中用户输入是否被正确转义

### 代码清理
- 将生产代码中的 `print()` 迁移为 `logger.info()` / `logger.debug()`
- 清理 `run_agent.py` 中遗留的损坏正则表达式
- 移除死代码和未使用的 import

### 架构拆分
- **BREAKING**: 将 `run_agent.py`（14292 行）中的 `AIAgent` 初始化逻辑抽取到 `agent/agent_init.py`
- **BREAKING**: 将 `gateway/run.py`（13202 行）中的消息路由逻辑抽取到 `gateway/message_handler.py`
- 将 `cli.py`（11841 行）中的辅助函数迁移到 `cli_helpers/` 已有目录

### 性能优化
- 模块级预编译正则表达式（已部分完成，补齐剩余文件）
- `os.getenv()` 调用缓存（已部分完成，补齐剩余文件）
- 修复 `smart_model_routing` 中 `resolve_turn_toolsets` 的 agent cache 签名问题（已修复，补充测试）

### 测试覆盖
- 为 `tools/registry.py` 添加单元测试
- 为 `tools/file_operations.py` 添加单元测试
- 为 `tools/web_tools.py` 添加单元测试

## Impact

- Affected specs: 安全审计、性能优化、架构重构
- Affected code:
  - `run_agent.py` — 修复正则、迁移 print、抽取初始化逻辑
  - `gateway/run.py` — 抽取消息路由逻辑
  - `cli.py` — 迁移辅助函数
  - `toolsets.py` — 修复 bare except
  - `tools/*.py` — 修复 except、添加测试
  - `hermes_cli/*.py` — 修复 shell=True
  - `tui_gateway/server.py` — 修复 shell=True

## ADDED Requirements

### Requirement: 安全加固
系统 SHALL 消除所有已知的命令注入风险和异常吞没问题。

#### Scenario: shell=True 替换
- **WHEN** 代码中存在 `subprocess.run(..., shell=True)` 调用
- **THEN** 替换为参数数组形式，消除 shell 注入风险

#### Scenario: 异常日志记录
- **WHEN** 捕获 `Exception` 且未记录日志
- **THEN** 添加 `logger.debug("...", exc_info=True)` 或更高级别的日志

### Requirement: 代码拆分
核心大文件 SHALL 按职责拆分为多个模块。

#### Scenario: run_agent.py 拆分
- **WHEN** `run_agent.py` 超过 10000 行
- **THEN** 将 AIAgent 初始化逻辑（~500 行）抽取到 `agent/agent_init.py`，确保现有 import 兼容

#### Scenario: gateway/run.py 拆分
- **WHEN** `gateway/run.py` 超过 10000 行
- **THEN** 将消息路由处理逻辑（~800 行）抽取到 `gateway/message_handler.py`

### Requirement: 测试覆盖
关键工具模块 SHALL 有单元测试覆盖。

#### Scenario: registry 测试
- **WHEN** 运行 `tests/tools/test_registry.py`
- **THEN** 覆盖 register、get_schema、resolve_toolset_alias 等核心方法

## MODIFIED Requirements

### Requirement: 异常处理
所有 `except Exception: pass` 必须至少包含 `logger.debug()` 记录异常上下文。对于预期内的异常使用具体异常类型替代宽泛的 `Exception`。

## REMOVED Requirements

无
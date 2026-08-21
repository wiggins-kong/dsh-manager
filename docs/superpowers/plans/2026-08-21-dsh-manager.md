# DSH 管理器 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个轻量 pywebview 桌面管理器，用于管理 DeepSeek Harness(dsh) 的版本查询、源码克隆、后台运行/停止、前端打开与代理设置。

**Architecture:** Python 3.12 后端(dsh_core.py) + pywebview(系统 WebView2)界面 + 原生 HTML/CSS/JS 单页。核心逻辑与界面分离，便于单元测试；PyInstaller 打包为独立 exe。

**Tech Stack:** Python 3.12, pywebview, requests, PyInstaller；前端原生 HTML/CSS/JS。

**Spec:** `docs/superpowers/specs/2026-08-21-dsh-manager-design.md`

## Global Constraints
- Python 3.12; 依赖用 `uv pip install`(本机 venv 无 pip)。
- 版本来源为 GitHub tags; 源码用 `git clone --depth 1 --branch <tag>`。
- 代理默认 `127.0.0.1:7897`，作用于 GitHub API 与 git clone，持久化到 config.json。
- 界面遵循用户偏好: 现代玻璃/Mica 风格、微软雅黑 Semibold 全局字体。
- 所有核心逻辑放在 dsh_core.py，main.py 只做 pywebview 桥接。

---

### Task 1: 项目骨架与依赖
**Files:**
- Create: `requirements.txt`
- Create: `.gitignore`

**Steps:**
- [ ] 写 `requirements.txt`: `pywebview>=5,<6`, `requests>=2.31`, `packaging>=23`
- [ ] 写 `.gitignore`: `__pycache__/`, `*.pyc`, `data/`, `build/`, `dist/`, `*.spec` 保留
- [ ] Commit: `git add -A && git commit -m "chore: 项目骨架与依赖"`

---

### Task 2: 核心逻辑 dsh_core.py
**Files:**
- Create: `dsh_core.py`
- Test: `tests/test_dsh_core.py`

**Interfaces (供后续 task 与 main.py 使用):**
- `class DSHManager:`
  - `__init__(self, data_dir: str | None = None)`
  - `get_proxy(self) -> dict | None`  (返回 requests proxies dict 或 None)
  - `git_proxy_args(self) -> list[str]`  (返回给 git 的 `-c http.proxy=...` 参数列表)
  - `load_config() / save_config()`
  - `fetch_versions(self, force: bool = False) -> list[str]`  (GitHub tags, 排序后返回)
  - `clone_tag(self, tag: str, on_log=None) -> str`  (克隆到 repos/dsh-<tag>, 返回路径)
  - `node_available(self) -> dict`  ({node: bool, pnpm: bool, node_version, pnpm_version})
  - `start_dsh(self, repo_dir: str, port: int = 3080, on_log=None) -> dict` (返 {pid, status})
  - `stop_dsh(self) -> bool`
  - `save_proxy(self, enabled, host, port)`
- `sort_versions(tags: list[str]) -> list[str]`  (版本号语义排序, 最高在前, 过滤非 semver/rc 保留但排后)

**Steps (TDD):**
- [ ] 写失败测试 `test_versions_sort`：给定乱序 tag 列表，断言 `sort_versions` 排序正确
- [ ] 实现 `sort_versions` (用 packaging.version)
- [ ] 跑测试通过
- [ ] 写失败测试 `test_proxy_args`：enabled+host/port 时 `git_proxy_args` 返回两条 `-c` 参数；禁用时返回 []
- [ ] 实现 `git_proxy_args` 与 `get_proxy`
- [ ] 跑测试通过
- [ ] 写失败测试 `test_node_available`、`test_clone_tag_command`(mock subprocess, 断言命令构造含 `--branch` 与 proxy 参数)、`test_fetch_versions`(mock requests)
- [ ] 实现对应方法
- [ ] 跑测试通过
- [ ] Commit: `git add -A && git commit -m "feat: dsh_core 核心逻辑 + 测试"`

---

### Task 3: 前端界面 (ui-ux-pro-max)
**Files:**
- Create: `web/index.html`, `web/style.css`, `web/app.js`

**Steps:**
- [ ] 用 ui-ux-pro-max 技能确定设计令牌(现代 Mica 玻璃风, 微软雅黑 Semibold, 深色/浅色)
- [ ] 实现 index.html 结构：顶部标题栏 + 左侧(版本列表/已下载) + 主区(选中版本详情与启动控制) + 底部日志
- [ ] 实现 style.css(玻璃拟态、圆角、主题色)
- [ ] 实现 app.js：调用 `window.pywebview.api.*` JSON bridge, 渲染版本列表、下载进度、启动/停止、代理设置、Node 检测提示
- [ ] Commit: `git add -A && git commit -m "feat: 前端界面"`

---

### Task 4: pywebview 入口 main.py + bridge
**Files:**
- Create: `main.py`

**Steps:**
- [ ] main.py 创建 pywebview 窗口, 加载 web/index.html
- [ ] 暴露 `class Api` 包装 DSHManager 方法(版本/克隆/启动/停止/代理/Node/路径)
- [ ] 启动时自动 node_check, 结果可推到前端
- [ ] 本机运行 `python main.py` 验证窗口打开、bridge 可调
- [ ] Commit: `git add -A && git commit -m "feat: pywebview 入口联动"`

---

### Task 5: 本机联调
**Steps:**
- [ ] 运行 `fetch_versions` 真实调用验证能拿到 tags(可走代理)
- [ ] 选择一个 tag 真实浅克隆验证
- [ ] 启动/停止 DSH 验证(或验证到 install/build 阶段)
- [ ] 验证 Node 检测
- [ ] 验证代理配置生效并持久化
- [ ] 记录结果

---

### Task 6: PyInstaller 打包
**Files:**
- Create: `build.spec`

**Steps:**
- [ ] 用 `uv pip install --python <venv> pyinstaller`
- [ ] 写 build.spec(onefile, 带 web/ 数据)
- [ ] 打包生成 exe
- [ ] 在无 Python 环境跑通(至少本机独立运行验证)
- [ ] Commit: `git add -A && git commit -m "build: pyinstaller 打包"`

---

### Self-Review 记录
- 覆盖: 查版本(2)、克隆(2)、运行/停止(2/4/5)、打开前端(4)、Node 检测(2/4)、代理(2/5)。
- 类型一致性: DSHManager 方法签名在各 task 一致。

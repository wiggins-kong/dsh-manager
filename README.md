# DSH 管理器 (DeepSeek Harness Manager)

一个轻量桌面管理器，用于管理 DeepSeek Harness（`deepseek-ai/deepseek-harness`，简称 DSH）的
**版本查询 → 源码下载 → 后台运行 → 打开前端 → 停止**，并支持**代理设置**（查版本、下源码走代理）。

界面基于 pywebview（系统 WebView2，无自带浏览器内核，体积/内存远小于 Electron），
打包为独立 exe，目标 Win10/Win11 无需预装 Python 即可运行管理器本身。

## 功能

- **发行版本**：从 GitHub 拉取 `deepseek-ai/deepseek-harness` 的 tag 列表（如 `dsh-v0.1.0-rc.8`），按版本号排序。
- **下载源码**：浅克隆（`git clone --depth 1 --branch <tag>`）指定版本的完整源码到 `data/repos/dsh-<版本>/`。
- **运行后端**：在克隆目录执行 `pnpm install` → `pnpm run build` → `pnpm dsh web`（默认端口 3080），实时回传日志。
- **打开前端**：系统默认浏览器打开 `http://127.0.0.1:3080`。
- **停止后端**：结束 DSH 进程树（Windows 用 `taskkill /T /F`）。
- **Node 检测**：检测 Node.js / pnpm，缺失时提示并可一键打开 nodejs.org 下载页。
- **代理设置**：开关 + 主机/端口（默认预填 Clash 混合端口 `127.0.0.1:7897`），对 GitHub API 与 git 均生效，持久化到 `data/config.json`。

## 开发运行

```bash
uv venv .venv
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
uv pip install --python .venv/Scripts/python.exe pytest pyinstaller   # 开发/打包
.venv/Scripts/python.exe -m pytest tests/ -q                          # 测试
.venv/Scripts/python.exe main.py                                      # 启动 GUI
```

> 提示：pnpm 在 Windows 安装为 `.cmd` 垫片，Python subprocess 需经 `cmd /c` 调用——已在代码中处理。

## 打包 exe

```bash
.venv/Scripts/pyinstaller.exe build.spec --noconfirm
```

产物在 `dist/DSH管理器.exe`，可拷到任意 Win10/Win11 直接运行（无需 Python）。
`data/` 目录会生成在 exe 同级，包含配置与下载的源码。

## 目录结构

```
main.py        # pywebview 入口 + JS API 桥
dsh_core.py    # 核心逻辑(版本/克隆/运行/停止/代理/配置)
web/           # 前端 (index.html / style.css / app.js)
data/          # 运行时生成: config.json + repos/(已下载源码), 已 gitignore
docs/          # 设计文档与实现计划
tests/         # 单元测试
```

## 说明

- 版本来源为 GitHub tags；源码必须来自 GitHub（含完整源码，可 build 运行）。
- 真正运行 DSH 的机器需安装 Node.js + pnpm（管理器只负责检测与引导）。
- 中国大陆网络环境建议开启代理（默认可直接填 Clash 端口 7897）。

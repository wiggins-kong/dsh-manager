# DEVELOPMENT.md — DSH-manager 开发进度 & 接手指南

> 本文件是项目的**开发交接单**: 记录当前进度、架构、约定、坑位, 以及如何在家/单位两台机器无缝接手。
> 更新的原则: 每次提交有意义改动后, 顺手更新本文件的「开发进度」与「下一步」两节, 并 push 到 GitHub。
> 仓库: (公开后请填写公开仓库地址)

---

## 一、项目是什么

**DSH-manager (DeepSeek Harness Manager)** — 一个轻量 Windows 桌面工具, 用来管理
[`deepseek-ai/deepseek-harness`](https://github.com/deepseek-ai/deepseek-harness) (简称 DSH) 的完整生命周期:

**版本查询 → 源码下载 → 后台运行 → 打开前端 → 停止**, 并支持**代理设置**(查版本、下源码走代理)。

- 界面基于 **pywebview**(系统自带 WebView2, 无独立浏览器内核, 内存/体积远小于 Electron)
- 打包为独立 exe, 目标 Win10/Win11 无需预装 Python 即可运行
- 开发语言: Python 3.12 + 原生 JS/CSS/HTML(无前端框架)

### 功能清单(已实现)

| 功能 | 说明 |
|------|------|
| 发行版本查询 | 拉取 `deepseek-ai/deepseek-harness` 的 tag 列表(形如 `dsh-v0.1.0-rc.8`), 语义化降序排序 |
| 源码下载 | 浅克隆 `git clone --depth 1 --branch <tag>`, 存到工作区 `dsh-<版本>/`, 已克隆的复用不重复下 |
| 后台运行 | 在克隆目录执行 `pnpm install → pnpm run build → pnpm dsh web`(默认端口 3080), 实时回传日志 |
| 打开前端 | 系统默认浏览器打开 `http://127.0.0.1:3080` |
| 停止后端 | 结束 DSH 进程树(Windows 用 `taskkill /T /F`, 并带 `_pid_alive` 兜底检测) |
| Node 检测 | 检测 Node.js / pnpm, 缺失时提示并一键打开 nodejs.org 下载页 |
| 代理设置 | 开关 + 主机/端口(默认预填 Clash 混合端口 `127.0.0.1:7897`), 对 GitHub API 与 git 均生效 |
| 主题 | 跟随系统(自动) / 深色 / 浅色, 实时切换 |
| 工作区路径 | 可自定义源码存放目录; 切换时处理旧源码(移动/删除/不动) |
| 删除源码 | 详情面板可删除已下载的源码目录(运行中的版本禁止删除) |
| 持久化 | 所有配置存 `data/config.json`(代理/缓存版本/主题/工作区/上次运行版本) |

---

## 二、架构与目录

```
main.py          # pywebview 入口 + JS API 桥 (class Api, window.pywebview.api.*)
dsh_core.py      # 核心逻辑: 配置/代理/版本/克隆/运行/停止/主题/工作区 (界面无关, 可单测)
web/             # 前端 (index.html / style.css / app.js)
data/            # 运行时生成: config.json + repos/(已下载源码) — 已 gitignore
docs/            # 设计文档与实现计划
tests/           # 单元测试 (pytest, 23 个全绿)
build.spec       # PyInstaller 打包配置 (onefile)
```

**分层原则**: `dsh_core.py` 不依赖任何 UI/界面, 所有可测逻辑在这层; `main.py` 只负责桥接 pywebview 与前端调用。跑得慢的操作(克隆/运行)通过 `Api._run_sync` 丢到工作线程 + `threading.Event` 等结果, 不阻塞 UI。

---

## 三、开发环境搭建

### 前置
- Windows + Python 3.12(本项目用 uv 管理 venv)
- git、Node.js + pnpm(`dsh web` 需要)

### 初始化(首次)
```bash
uv venv .venv                                # 需要 python 3.12
uv pip install --python .venv/Scripts/python.exe -r requirements.txt
uv pip install --python .venv/Scripts/python.exe pytest pyinstaller   # 开发/打包
```

> ⚠️ 用 `uv` 装依赖时, 若本机 hermes 的 python 有问题, 可改用系统 python(`python -m venv`)。

### 跑测试
```bash
.venv/Scripts/python.exe -m pytest tests/ -q
# 期望: 23 passed
```

### 启动 GUI(开发)
```bash
.venv/Scripts/python.exe main.py
# 或开启调试控制台: DSH_DEBUG=1 .venv/Scripts/python.exe main.py
```

### 打包 exe
```bash
.venv/Scripts/pyinstaller.exe build.spec --noconfirm
# 产物: dist/DSH-manager.exe (onefile, 可拷到任意 Win10/11 直接运行)
```

---

## 四、代码接手速览(关键点)

### dsh_core.py(DSh Manager 类)
- `fetch_versions(force)`: 走 GitHub tags API; 命中 `cached_versions` 缓存时不再请求; `force=True` 强制刷新。
- `clone_tag(tag, on_log)`: `--depth 1 --branch` 浅克隆到 `repo_dir_for(tag)` = `data/repos/dsh-<去前缀后的版本>`, 写 `.dsh-tag` 标记文件; 已存在 `.git` 则直接复用。
- `repo_dir_for` **必须**去掉 `dsh-` 前缀再拼一次(`dsh-{_dir_clean(tag)}`),避免重复前缀导致"未下载"误报 ← 历史坑, 见 commit 810358f。
- `start_dsh(repo_dir, port, on_log)`: 先 `pnpm install` + `pnpm run build`, 再后台 `pnpm dsh web --port`; 记 `_proc`/`_proc_pid`, 写 `last_tag`。
- `stop_dsh()`: `taskkill /T /F` 杀进程树 + `_proc` 兜底 terminate; `running` 属性用 Windows `OpenProcess` 查 STILL_ACTIVE。
- `preview_workspace` / `apply_workspace(path, on_old)`: 换工作区; `on_old ∈ move|delete|leave` 决定旧源码处置。
- 代理: `get_proxy()` 返回 dict; `git_proxy_args()` 拼 `-c http.proxy=...` 供 git 用; 请求走 `requests(proxies=...)`。

### main.py(Api 桥)
- 每个方法名即 `window.pywebview.api.<名>`。耗时操作包一层 `_run_sync`。
- `pick_folder(initial_dir)`: 调 `win.create_file_dialog(FOLDER_DIALOG)` → 原生目录选择框(必须经 js_api 在 GUI 线程, 否则弹不出来)。
- `get_state()`: 前端首屏一次性拉取的聚合状态。

### web/(前端)
- `index.html`: 结构; `style.css`: 玻璃/现代风; `app.js`: 逻辑 + `window.pywebview.api.*` 调用。
- 与 pywebview 桥就绪的时序已处理(见 commit c859b5b), 避免开局把 pywebview 误判成普通浏览器。

---

## 五、约定 & 坑位(接手法则)

1. **语言**: 代码注释、前端文案、提交信息一律用**中文**(面向中文用户/开发者)。
2. **UI 风**: 干净的玻璃/现代/马赛克风(Mica), **不要**红章/古风花哨皮肤; 字体偏好微软雅黑 Semibold(MsyhSb); 主题通过设置弹窗切换, 不在工具栏加开关。
3. **Windows pnpm 是 `.cmd` 垫片**: 直接 subprocess 会找不到, 必须经 `cmd /c` —— 由 `_spawn_cmd()` 统一处理; 新增外部命令调用时注意。
4. **子进程一律隐藏窗口**: Windows 下 Popen 必须带 `CREATE_NO_WINDOW`(`_hidden_popen_kwargs()`), 否则 cmd/git 会弹出黑窗。
5. **pnpm 11 运行前会做依赖状态检查**: `pnpm <script>`(如 `pnpm dsh web`)前 `runDepsStatusCheck` 发现 node_modules 与 lockfile 不同步时自动 install; 若需移除 modules 目录则要求 TTY 确认, 无 TTY 子进程直接 abort(`ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY`), 表现为"后端进程在启动过程中退出"。所有 pnpm 子进程必须带环境变量 `pnpm_config_verify_deps_before_run=false`(`_pnpm_env()`)跳过检查, 依赖是否就绪交给管理器自身的 install/build 步骤。
6. **js_api 不能传 JS 函数**: pywebview 的 js_api 参数走 JSON 序列化, JS 函数会变 `null`(on_log 回调从未生效!)。日志推送必须走 `Api._emit_log` → `window.evaluate_js` → 前端 `window.__dsh_log`。新增强日志功能时照此模式。
7. **目录名前缀**: 已下载源码目录统一 `dsh-<版本>`; 命名直接决定"是否已下载"判断, 改动须同步 `repo_dir_for` 与测试 `test_repo_dir_for_strips_prefix`。
8. **配置合并**: `_load_config` 用 `default_config` + `update` 合并, 新增配置项记得加进 `_default_config()`。
9. **测试先行**: 核心逻辑改完必跑 `pytest`; 有 DOM/时序类问题时多写桥接层测试。

---

## 六、开发进度(截至 v1.0)

按时间倒序, 需求在 `docs/` 设计文档与 git 历史可见。

- **(v1.0 发布版)**: ① 新增「删除源码」; ② 子进程加 `CREATE_NO_WINDOW` 不再弹黑窗; ③ 修复日志机制(js_api 传 JS 函数会变 null, 改 `_emit_log` evaluate_js 推送, 进度实时可见); ④ `start_dsh` 加进程存活检查 + 端口就绪探测, 失败明确报错; ⑤ clone/install/build/启动分阶段日志; ⑥ `clone_tag` 复用判定改为"package.json + .git 都不缺", 残缺下载残留自动清理重下; ⑦ 智能跳过 install/build, 二次运行秒级启动; ⑧ 运行不再自动弹浏览器(`--no-open`); ⑨ 启动前清理占用端口的残留进程 + 关窗自动停后端(修"前端打不开"); ⑩ 删除源码用 `_rmtree_force` 强删只读 .git 文件, 不留半截目录; ⑪ 自研「中枢调度」应用图标(exe + 窗口标题栏 + 界面左上角三处统一), `gen_icon.py` 可复现。
- **0e965f2 / 99f5173 (主题 + 工作区)**: 前端主题支持 `system`(跟随系统实时切换); 源码路径设置 + 原生目录浏览; 切换工作区时弹窗处理旧源码(移动/删除/不动), 均配测试。
- **c859b5b (桥接时序)**: 修复 pywebview 桥就绪时序, 不再开局误判为普通浏览器导致点击/刷新/设置失效。
- **714a912 (兼容与打包)**: 版本识别支持官方 `dsh-` 前缀; Windows pnpm `.cmd` 垫片; clone 复用与 `.dsh-tag` 标记; 补齐 README 与打包配置。
- **7d88a03 / 0e0e0c9 (骨架)**: 前端界面 + pywebview 入口; dsh_core 核心逻辑 + 测试。
- **35ddfc8 (设计)**: DSH-manager 设计文档。
- **810358f**: 修复 `Api.start` 源码目录重复 `dsh-` 前缀导致的"未下载"误报; 待克隆路径显示真实工作区。

**质量状态**: `pytest` 34/34 通过; exe 已集成自研图标; GitHub Actions 自动构建发布 Release。

**v1.0.1 (pnpm 11 兼容修复)**: `pnpm dsh web` 启动报 "后端进程在启动过程中退出"。根因: pnpm 11 运行前做依赖状态检查(`runDepsStatusCheck`), 发现 node_modules 与 lockfile 不同步时自动 install, 需移除 modules 目录时要求 TTY 确认, 而管理器子进程无 TTY 直接 abort(`ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY`)。修复: 所有 pnpm 子进程带 `pnpm_config_verify_deps_before_run=false`(`_pnpm_env()`), 跳过运行前检查。含端到端验证(真实启动 dsh web 成功)。

---

## 七、下一步(候选 TODO)

- [ ] 运行状态的前端实时展示(当前是轮询 `get_state`, 可考虑事件推送)
- [ ] 版本列表分页(拉取超过 100 个 tag 时)
- [ ] 更新检查: 应用自身版本 / 提示新版本
- [ ] 运行端口冲突检测与自动换端口
- [ ] 前端本地化/错误提示更友好(运行时 Node 崩溃时捕获具体原因)
- [ ] 打包后 `data/` 位置由 exe 同级改为用户目录的取舍(当前 exe 同级, 提升权限时需要)

> 建议每次开工先看 git log 与「七、下一步」, 主动确认当前分支状态再动手。

---

## 八、多机协作(在家/单位无缝接手)

1. **仓库即真相**: 所有代码以 **公开的 GitHub 仓库** 为准 (公开后请在此处填写仓库地址)。两处 `git pull` / `push` 保持同步。
2. 接手第一句可对 Hermes 说:
   > 继续开发 DSH-manager, 项目在 `E:\...dsh-manager`。先读 `DEVELOPMENT.md` 和 `README.md` 了解现状, `git pull` 对齐, 然后按「七、下一步」继续。
3. git 流程: `git pull` 开始 → 改 → 测试通过 → `git add` → `git commit`(中文信息, 遵循 `feat/fix/docs/style/...` 前缀)→ `git push`。
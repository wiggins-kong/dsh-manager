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
| 打开前端 | 系统默认浏览器打开后端地址;新版 dsh web 带进程级 token 认证,自动解析并使用带 token 的认证 URL(旧版回退裸地址) |
| 停止后端 | 结束 DSH 进程树(Windows 用 `taskkill /T /F`, 并带 `_pid_alive` 兜底检测) |
| Node 检测 | 检测 Node.js / pnpm, 缺失时提示并一键打开 nodejs.org 下载页 |
| 代理设置 | 开关 + 主机/端口(默认预填 Clash 混合端口 `127.0.0.1:7897`), 对 GitHub API 与 git 均生效 |
| 主题 | 固定跟随系统深浅色(实时切换, 无设置项) |
| 工作区路径 | 可自定义源码存放目录; 切换前全量预检(嵌套拒绝/同名完整副本跳过视为已迁移/残缺副本逐个询问), 后端运行中先停后端再迁移, 模态进度弹窗逐版本反馈, 失败只清理自己复制出的半成品 |
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
tests/           # 单元测试 (pytest, 46 个全绿)
CHANGELOG.md     # 版本更新日志(Keep a Changelog 格式, 发版同步 APP_VERSION + git tag)
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
# 期望: 46 passed
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
- `start_dsh(repo_dir, port, on_log)`: 先 `pnpm install` + `pnpm run build`, 再后台 `pnpm dsh web --port`; 记 `_proc`/`_proc_pid`, 写 `last_tag`。pump 线程逐行解析输出中的 `dsh web: http://…?token=…`(`_parse_auth_url`, 兼容 ANSI 颜色码)存入 `_auth_url`; 端口就绪后最多等 5s 让 token 行出现再打就绪日志。
- `web_url()`: 优先返回带 token 的认证 URL `_auth_url`(新版 dsh web 访问裸地址一律 401), 未解析到/旧版回退 `http://127.0.0.1:<port>`。启动/停止时重置。
- `stop_dsh()`: `taskkill /T /F` 杀进程树 + `_proc` 兜底 terminate; `running` 属性用 Windows `OpenProcess` 查 STILL_ACTIVE。
- `plan_workspace_move(path)`: 迁移预检(不动文件不停后端), 返回 `{relation, conflicts, movable, skippable}`; relation ∈ ok|nested|same, conflicts 每项 `{name, complete}`(complete = 目标有 package.json)。
- `apply_workspace(path, on_old, on_progress, on_cleanup, resolutions)`: 换工作区; `on_old ∈ move|delete|leave` 决定旧源码处置。move 时逐项处置: 目标不存在→移动, 完整副本→跳过(视为已迁移), 残缺副本→必须给 `resolutions[name] ∈ delete_copy|skip` 否则拒绝。只在实际动旧路径文件时才停后端。`on_progress/on_cleanup(done,total,name)` 逐版本进度与清理进度回调。失败清理 `_cleanup_partial` 只删本次尝试新建且未移完的目标目录, 预存在目录绝不碰。
- 代理: `get_proxy()` 返回 dict; `git_proxy_args()` 拼 `-c http.proxy=...` 供 git 用; 请求走 `requests(proxies=...)`。

### main.py(Api 桥)
- 每个方法名即 `window.pywebview.api.<名>`。耗时操作包一层 `_run_sync`。
- 进度/日志推送统一走 `_emit_js(fn, args)` → `window.evaluate_js`; 迁移进度为 `_emit_ws_progress`/`_emit_ws_cleanup` → 前端 `window.__dsh_ws_progress`/`__dsh_ws_cleanup`。
- `plan_workspace(path)` / `apply_workspace(path, on_old, resolutions)`: 预检与执行工作区迁移; `resolutions` 是前端逐个询问残缺副本后收集的 `{目录名: delete_copy|skip}`。
- `pick_folder(initial_dir)`: 调 `win.create_file_dialog(FOLDER_DIALOG)` → 原生目录选择框(必须经 js_api 在 GUI 线程, 否则弹不出来)。
- `get_state()`: 前端首屏一次性拉取的聚合状态。
- **原生标题栏染色**: `_apply_titlebar_theme(theme)` 走 DWM(`DWMWA_CAPTION_COLOR=35` / `TEXT_COLOR=36` / `BORDER_COLOR=34`, Win11+; 旧 Win10 回退沉浸式深色模式), 配色表 `_TITLEBAR_COLORS` 与 style.css 各主题令牌一致。前端主题变化时经 `Api.sync_theme(mode)` 通知重染; 窗口 shown 时先按系统深浅色兜底。

### web/(前端)
- `index.html`: 结构; `style.css`: 玻璃/现代风; `app.js`: 逻辑 + `window.pywebview.api.*` 调用。
- 与 pywebview 桥就绪的时序已处理(见 commit c859b5b), 避免开局把 pywebview 误判成普通浏览器。

---

## 五、约定 & 坑位(接手法则)

1. **语言**: 代码注释、前端文案、提交信息一律用**中文**(面向中文用户/开发者)。
2. **UI 风**: 干净的玻璃/现代/马赛克风(Mica), **不要**红章/古风花哨皮肤; 字体偏好微软雅黑 Semibold(MsyhSb); 主题固定跟随系统, 无设置项。
3. **Windows pnpm 是 `.cmd` 垫片**: 直接 subprocess 会找不到, 必须经 `cmd /c` —— 由 `_spawn_cmd()` 统一处理; 新增外部命令调用时注意。
4. **子进程一律隐藏窗口**: Windows 下 Popen 必须带 `CREATE_NO_WINDOW`(`_hidden_popen_kwargs()`), 否则 cmd/git 会弹出黑窗。
5. **pnpm 11 运行前会做依赖状态检查**: `pnpm <script>`(如 `pnpm dsh web`)前 `runDepsStatusCheck` 发现 node_modules 与 lockfile 不同步时自动 install; 若需移除 modules 目录则要求 TTY 确认, 无 TTY 子进程直接 abort(`ERR_PNPM_ABORTED_REMOVE_MODULES_DIR_NO_TTY`), 表现为"后端进程在启动过程中退出"。所有 pnpm 子进程必须带环境变量 `pnpm_config_verify_deps_before_run=false`(`_pnpm_env()`)跳过检查, 依赖是否就绪交给管理器自身的 install/build 步骤。
6. **js_api 不能传 JS 函数**: pywebview 的 js_api 参数走 JSON 序列化, JS 函数会变 `null`(on_log 回调从未生效!)。日志推送必须走 `Api._emit_log` → `window.evaluate_js` → 前端 `window.__dsh_log`。新增强日志功能时照此模式。
7. **目录名前缀**: 已下载源码目录统一 `dsh-<版本>`; 命名直接决定"是否已下载"判断, 改动须同步 `repo_dir_for` 与测试 `test_repo_dir_for_strips_prefix`。
8. **配置合并**: `_load_config` 用 `default_config` + `update` 合并, 新增配置项记得加进 `_default_config()`。
9. **测试先行**: 核心逻辑改完必跑 `pytest`; 有 DOM/时序类问题时多写桥接层测试。
10. **原生标题栏颜色不跟随系统而跟随应用主题**: pywebview 不暴露标题栏配色, 由 main.py 走 DWM 手动染色(见「四、main.py」)。改界面主题配色时必须同步 `_TITLEBAR_COLORS`; 前端主题变化必须调用 `Api.sync_theme`, 否则标题栏与界面割裂。
11. **新版 dsh web 有浏览器会话认证**: 每次启动生成进程级 launch token, 只接受 `dsh web` 打印的 `http://…/?token=…` URL(访问后种 cookie 再 303 跳回 `/`), 裸地址一律 401 "dsh web authentication required"。打开前端的 URL 必须来自 `web_url()`(内部优先 `_auth_url`), 不要手拼裸地址。
12. **失败清理绝不删预存在的目录**: 工作区迁移失败后只允许清理"本次尝试自己复制出来的"半成品; 用户手动复制到新路径的文件(哪怕残缺)只能经用户逐个确认(`resolutions`)后删除。历史事故: 清理逻辑看到"旧路径原件在 + 新路径目录也在"就把用户完整副本当半成品删光。
13. **打包机 pywebview 版本必须锁 `>=5,<6`**: requirements.txt 已锁; 但若手动 `pip install pywebview` 装到 6.x 再打包, exe 一打开窗口就无响应(pywebview 6 Windows 平台实现变化)。打包前先 `pip show pywebview` 确认是 5.x。
14. **后台进程生命周期长于 start_dsh**: 日志文件在启动流程返回后即关闭, 而 pump 线程继续读后端输出——`_make_logging` 对已关闭的 `log_file.write` 静默容忍(ValueError), 否则 pump 线程死亡、日志断流。

---

## 六、开发进度(截至 v1.1.0, 2026-09-06 发版)

按时间倒序, 需求在 `docs/` 设计文档与 git 历史可见。

- **(v1.1 后续, 本次开发)**: ① **dsh web 认证适配**: 解析后端输出的带 token 认证 URL(`_parse_auth_url`/`_auth_url`/`web_url`), 修「打开前端」401 "authentication required"; 顺带修 pump 线程因日志文件提前关闭而死亡的 bug(日志只剩前几行)。② **工作区迁移重做**(grill-me 流程确认方案): 预检 `plan_workspace_move`(嵌套拒绝/完整副本跳过视为已迁移/残缺副本逐个询问), 后端运行中先停再迁, 全移走才算成功, 失败清理只删本次复制出的半成品(修"用户手动副本被误删"的数据毁灭事故), 迁移与清理均带模态逐版本进度(`_emit_ws_progress`/`_emit_ws_cleanup` → 前端进度弹窗)。新增 9 个测试覆盖。③ **日志区操作可发现性优化**(grill-me 流程确认方案): 「清空」/「打开日志目录」由灰色纯文字链接(`link-btn`, 已删)升级为小型描边按钮(`btn btn-sm`, 常显), 文案改「打开日志目录」+ 明确悬浮提示, 修日志头部 `space-between` 三元素散落的布局(标题靠左、按钮成组靠右)。
- **(v1.1 功能)**: 单实例锁; 后端日志落盘 + 「打开日志目录」; 端口冲突自动换端口; 原生标题栏按主题染色 + 顶栏去重; 日志字体微软雅黑; 新增 CHANGELOG.md。

- **(v1.0 发布版)**: ① 新增「删除源码」; ② 子进程加 `CREATE_NO_WINDOW` 不再弹黑窗; ③ 修复日志机制(js_api 传 JS 函数会变 null, 改 `_emit_log` evaluate_js 推送, 进度实时可见); ④ `start_dsh` 加进程存活检查 + 端口就绪探测, 失败明确报错; ⑤ clone/install/build/启动分阶段日志; ⑥ `clone_tag` 复用判定改为"package.json + .git 都不缺", 残缺下载残留自动清理重下; ⑦ 智能跳过 install/build, 二次运行秒级启动; ⑧ 运行不再自动弹浏览器(`--no-open`); ⑨ 启动前清理占用端口的残留进程 + 关窗自动停后端(修"前端打不开"); ⑩ 删除源码用 `_rmtree_force` 强删只读 .git 文件, 不留半截目录; ⑪ 自研「中枢调度」应用图标(exe + 窗口标题栏 + 界面左上角三处统一), `gen_icon.py` 可复现。
- **0e965f2 / 99f5173 (主题 + 工作区)**: 前端主题支持 `system`(跟随系统实时切换); 源码路径设置 + 原生目录浏览; 切换工作区时弹窗处理旧源码(移动/删除/不动), 均配测试。
- **c859b5b (桥接时序)**: 修复 pywebview 桥就绪时序, 不再开局误判为普通浏览器导致点击/刷新/设置失效。
- **714a912 (兼容与打包)**: 版本识别支持官方 `dsh-` 前缀; Windows pnpm `.cmd` 垫片; clone 复用与 `.dsh-tag` 标记; 补齐 README 与打包配置。
- **7d88a03 / 0e0e0c9 (骨架)**: 前端界面 + pywebview 入口; dsh_core 核心逻辑 + 测试。
- **35ddfc8 (设计)**: DSH-manager 设计文档。
- **810358f**: 修复 `Api.start` 源码目录重复 `dsh-` 前缀导致的"未下载"误报; 待克隆路径显示真实工作区。

**质量状态**: `pytest` 46/46 通过; exe 已集成自研图标; GitHub Actions 自动构建发布 Release。

**v1.1 (功能增强, 开发中)**:
- **单实例锁**: Windows Named Mutex(`CreateMutexW`) 防双开, 第二实例自动激活已有窗口并退出。
- **后端日志落盘**: 每次运行追加写入 `data/logs/dsh-<tag>-<时间戳>.log`, install/build/运行阶段全量记录; 前端新增「打开日志目录」按钮。
- **端口冲突自动换端口**: `_resolve_port` 从 3080 起探测, 残留 DSH 进程(taskkill 可杀)直接释放, 外部进程占用则递增换端口(3081/3082…, 最多 10 次); `get_state` 返回实际 `running_port` 供前端显示。
- **原生标题栏按主题染色 + 顶栏去重**: 标题栏用 DWM 染成与应用当前主题一致的底/文字/边框色, 深浅色实时跟随; 应用内顶栏删除重复的 "DSH-manager" 大标题(原生标题栏承担), 只留 logo + "DeepSeek Harness" 副标。
- **日志字体**: 日志区由等宽字体改为微软雅黑, 与全局一致。
- **CHANGELOG.md**: 新增版本更新日志, 与 `APP_VERSION` / git tag 发版流程绑定。

---

## 七、下一步(候选 TODO)

- [ ] 运行状态的前端实时展示(当前是轮询 `get_state`, 可考虑事件推送)
- [ ] 工作区迁移的跨盘复制粒度进度(当前按版本目录粒度, 单个 node_modules 复制几分钟时进度条会停一格不动)
- [ ] 版本列表分页(拉取超过 100 个 tag 时)
- [ ] 更新检查: 应用自身版本 / 提示新版本
- [ ] 前端本地化/错误提示更友好(运行时 Node 崩溃时捕获具体原因)
- [ ] 打包后 `data/` 位置由 exe 同级改为用户目录的取舍(当前 exe 同级, 提升权限时需要)

> 建议每次开工先看 git log 与「七、下一步」, 主动确认当前分支状态再动手。

---

## 八、多机协作(在家/单位无缝接手)

1. **仓库即真相**: 所有代码以 **公开的 GitHub 仓库** 为准 (公开后请在此处填写仓库地址)。两处 `git pull` / `push` 保持同步。
2. 接手第一句可对 Hermes 说:
   > 继续开发 DSH-manager, 项目在 `E:\...dsh-manager`。先读 `DEVELOPMENT.md` 和 `README.md` 了解现状, `git pull` 对齐, 然后按「七、下一步」继续。
3. git 流程: `git pull` 开始 → 改 → 测试通过 → `git add` → `git commit`(中文信息, 遵循 `feat/fix/docs/style/...` 前缀)→ `git push`。
# DSH 管理器 · 设计文档

- 日期: 2026-08-21
- 作者: Hermes Agent (为 wiggins 构建)
- 状态: 已批准

## 1. 背景与目标

DeepSeek Harness (`dsh`, 仓库 `deepseek-ai/deepseek-harness`) 是 DeepSeek 官方的
开源 agent harness(Node/pnpm 生态)。用户希望有一个桌面管理器，用来：

1. 查看 DSH 所有已发布的版本(GitHub tags);
2. 把指定版本的完整源码拉到本地(git clone + checkout);
3. 运行 DSH 后端服务(后台进程);
4. 打开前端网页(http://127.0.0.1:3080);
5. 关闭 DSH 后端服务(杀进程树);
6. 可设置代理，让"查版本 / 下载源码"走代理(中国大陆网络环境需要)。

目标形态: 一个轻量桌面应用，用 PyInstaller 打成独立 exe，目标
Win10/Win11 机器无需预装 Python 即可运行管理器本身。DSH 的运行仍需
目标机安装 Node.js + pnpm，管理器负责检测并在缺失时引导下载。

## 2. 技术栈

| 层 | 选型 | 理由 |
|---|---|---|
| 主逻辑 | Python 3.12 | 网络/进程/git 原生顺手, 复用用户 uv 环境 |
| 界面 | pywebview 3 (系统 WebView2) | 不带自带浏览器内核, 内存约为 Electron 1/3 |
| 前端 | 原生 HTML/CSS/JS 单页 | 无框架, 省体积 |
| 打包 | PyInstaller → 单文件 exe | 目标机零依赖 |

## 3. 架构与模块

```
dsh-manager/
  main.py          # pywebview 入口 + JS API 桥
  dsh_core.py      # 核心逻辑: 版本/克隆/运行/停止/代理/配置
  web/
    index.html
    style.css
    app.js
  requirements.txt
  build.spec       # PyInstaller 配置
  config.json      # 运行时生成(持久化)
  tests/
    test_dsh_core.py
```

### 3.1 版本查询 (versions)
- 调 `GET https://api.github.com/repos/deepseek-ai/deepseek-harness/tags?per_page=100`
- 结果按版本号语义排序(最高在前)
- 可选走代理; 结果缓存到 config 以省重复请求

### 3.2 源码下载 (repo)
- 在工作区 `<data>/repos/dsh-<tag>/` 执行
  `git clone --depth 1 --branch <tag> <repo_url> <dir>`
- 浅克隆单 tag; 克隆时按配置注入 `http.proxy` / `https.proxy`

### 3.3 运行 (run)
- 在克隆目录执行 `pnpm install` → `pnpm run build` → `pnpm dsh web`
  (默认端口 3080), 作为后台子进程, 实时回传 stdout/stderr 到 UI 日志

### 3.4 打开前端 (open_web)
- 用系统默认浏览器打开 `http://127.0.0.1:<port>`

### 3.5 停止 (stop)
- 杀掉 DSH 进程树; Windows 用 `taskkill /T /F /PID <pid>`

### 3.6 Node 检测 (node_check)
- 启动时执行 `node --version` 与 `pnpm --version`
- 缺失 → UI 提示 + 按钮打开 nodejs.org 下载页

### 3.7 代理设置 (proxy)
- 设置: 开关 + 主机 + 端口 (默认 127.0.0.1:7897)
- 持久化到 config.json; 对 GitHub API 请求与 git clone 均生效

### 3.8 配置持久化 (config)
- `<data>/config.json`, 字段:
  `proxy: {enabled, host, port}`, `workspace`, `cached_versions`, `last_tag`
- data 目录默认在 exe 同级下 `data/`, 便于便携

## 4. 数据流
- 前端 JS → pywebview jsbridge → Python API → 执行 → 结果/日志弹回 UI
- 长操作(克隆/启动)通过 log 回调实时推送给界面

## 5. 界面结构 (见 ui-ux-pro-max 技能细化)
- 主区: 版本列表 | 已下载版本 | 启动控制(运行/打开前端/停止) | 设置
- 遵循用户偏好: 现代玻璃/Mica 风格, 微软雅黑半粗(微软雅黑 Semibold)全局字体

## 6. 错误处理
- GitHub 连不上 → 提示建议开启代理
- git 失败 → 显示 stderr 日志
- 端口占用 → 明确提示
- 目录已存在 → 提示复用或覆盖

## 7. 测试
- 对 dsh_core 的网络/版本解析/克隆命令构造/进程管理做单元测试
- 网络相关用 mock, 不依赖真实网络

## 8. 验收标准
- [ ] 管理器 exe 能在无 Python 的 Win10/11 上运行
- [ ] 能列出 GitHub tags 版本
- [ ] 能克隆指定版本源码
- [ ] 能后台运行 DSH、打开前端、停止后端
- [ ] 能检测 Node 缺失并引导下载
- [ ] 代理设置对查版本和 git 生效且持久化

# Windows 使用说明

适配目标为 Windows 10 / 11 x64、Python 3.12（64 位）。使用本机浏览器，不需要 WSL、Docker、Node.js 或本地大模型。日常使用推荐新版 Edge 或 Chrome。

## 第一次使用

1. 安装 [Python 3.12 Windows 版本](https://www.python.org/downloads/windows/)，安装时勾选 Python Launcher 和 Add python.exe to PATH。
2. 将源代码 ZIP 完整解压到自己的本地目录，例如 `D:\PersonalAgent`。不要在压缩包预览窗口中启动。中文和带空格的路径可用；暂不支持网络共享目录和 Windows ARM64。
3. 双击 `安装依赖.cmd`，等待出现“安装完成”。首次安装需要联网下载开源 Python 依赖；不会安装 Ollama、推理大模型、系统服务或开机启动项，不需要管理员权限。
4. 双击 `启动个人Agent.cmd`，默认浏览器打开 `http://127.0.0.1:8787/`。
5. 双击 `停止个人Agent.cmd` 停止，或用 `查看运行状态.cmd` 检查。关闭命令窗口、浏览器后后台服务继续运行；休眠和关机会中断提醒。

API 在“设置”中填写地址、Key、模型名称，默认关闭。原有日历、待办、资料库、助手和自动化入口不变。没有配置的外部服务不会自动接通。

## 从 Mac 迁移

源代码压缩包不含个人数据和 Key。若要保留数据，先在 Mac 停止工作台，再将整个 `output/runtime` 复制到 Windows 项目的同名位置，保留文件结构。不要同时运行两份数据库，不要只复制单个 SQLite 文件或在服务运行时直接复制。

不要复制 Mac 的 `.venv`；在 Windows 重新安装依赖。如果已经复制，可先将 `.venv` 重命名备份，再运行安装入口。依赖安装和启停脚本不会删除 `output/runtime`。

API/日历/通知配置仍是本机明文，迁移的目录包含凭据。Windows 文件沿用所在目录的访问权限，不把 POSIX 的 `0600` 当作 Windows 访问控制。请放在自己的用户目录，避免共享盘、公共目录及自动云同步目录；不需要为了运行放开防火墙或使用管理员权限。

## 命令行

以下命令在项目目录的 PowerShell 中运行，无需激活虚拟环境、无需修改脚本执行策略。

```powershell
.\.venv\Scripts\python.exe -X utf8 -B code\manage_service.py start --open
.\.venv\Scripts\python.exe -X utf8 -B code\manage_service.py status
.\.venv\Scripts\python.exe -X utf8 -B code\manage_service.py stop
```

端口冲突时，为所有启停命令指定同一个端口，例如 `--port 8790`。脚本只管理经过验证的本项目服务，不按端口强制结束其他程序。

隔离调试或使用其他数据目录时，给管理命令加 `--data-dir "D:\Agent Test\data"`；启停和查询须使用同一端口与目录。前台调试可用：

```powershell
.\.venv\Scripts\python.exe -X utf8 -B code\run.py web --port 8788 --data-dir "D:\Agent Test\data"
```

前台服务在原窗口按 Ctrl+C 停止。后台停止凭据保存在数据目录的 `service.json`，不要在服务运行时删掉它。若丢失凭据，脚本会拒绝强制结束进程；请在任务管理器核对进程后手动停止。

## 常见问题

- **找不到 Python**：安装 Python 3.12 x64，关闭后重开命令窗口；使用 `py -3.12 --version` 检查。安装入口也可使用 PATH 中的 Python 3.12。
- **安装失败**：保留窗口中的实际报错，检查网络、代理、磁盘空间及目录权限后重新安装。脚本不会自动切换第三方镜像、安装编译器或改变系统设置。第三方依赖的 Windows 安装仍需实机验证。
- **启动失败**：查看 `output/service.log`，或先运行状态入口。端口被其他服务占用时会提示换端口。
- **页面空白或旧样式**：确认服务启动后打开回环地址，按 Ctrl+F5 刷新。不要直接双击 HTML 文件。
- **时区错误**：重新运行依赖安装；本版明确依赖 `tzdata`，Windows 不需要手动安装系统 IANA 时区库。
- **外部 MCP 桥接**：命令和参数在 JSON 中分开填写，使用 Windows 实际可执行文件路径；反斜杠需写成 `\\`。优先直接指定 `python.exe` / `node.exe`，不要依赖 Mac 的 `.venv/bin/python`、zsh 或 shell 命令串。

## 验证范围

本次开发在 macOS 完成，已加入可在两个系统运行的回归测试与 `.github/workflows/compatibility.yml`。本机验证中文路径、配置存储、时区、管道超时、后台启停、数据保留和原有业务；对 Windows 特定分支做针对性模拟。当前没有 Windows 实机或已运行的 Windows CI 结果，不能视为完成实机验收。已查询 PyPI 官方元数据，现有环境中的 28 个平台相关依赖均有适用于 Windows x64 / Python 3.12 的 wheel；这不替代完整安装和运行验证。

Windows 验收应包含首次依赖安装、双击启停、中文路径、日程/待办编辑、文件解析、API 配置保存、关闭命令窗口后服务仍在运行，以及无冲突的正常停止。CI 会运行依赖安装和功能回归，不会代替双击启动及实际浏览器操作。

实现依据：[Python 子进程说明](https://docs.python.org/3.12/library/subprocess.html)、[Windows 管道与 select 的限制](https://docs.python.org/3.12/library/select.html)、[zoneinfo 的跨平台时区说明](https://docs.python.org/3.12/library/zoneinfo.html)。

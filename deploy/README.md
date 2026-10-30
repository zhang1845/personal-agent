# Daynest · 完整在线版部署

当前提供可部署代码、单用户登录、Docker 持久化数据卷及 GitHub 构建检查。**尚未部署公网服务**：仍需选择托管账号或服务器、配置 HTTPS 地址和持久化存储。没有开通付费资源。

GitHub 保存源码并运行检查，不托管此 Python 后端。Pages 只能发布静态网页。不要将本机版改绑公网或用临时隧道当作长期部署。

## 费用与平台要求

主机需支持 Python 3.12 或 Docker、持续运行的单实例进程和可写的持久化本地磁盘。已有免费资源可以使用，但“免费、长期、持续运行、数据不丢失”需按平台实际额度确认。本项目不会创建账号、绑定支付方式或自动升级套餐。

Render 免费 Web Service 会在空闲15分钟后休眠，重启/休眠会丢失本地 SQLite；免费服务不能挂持久化磁盘。免费 Postgres 也有30天期限，且本项目没有迁移成 Postgres。它不能直接作为当前正式数据的长期宿主。[平台文档](https://render.com/docs/free)

## 模型 Key 的去向

- 不读取或复制你电脑 `output/runtime/model-api.json` 中的 Key。
- Key 不放 GitHub 源码、Actions Secrets、Docker 构建参数、前端或镜像。
- 首次云端部署使用空数据。通过 HTTPS 登录后，在“设置 → 模型 API”单独填写 Key，保存到云端数据卷的 `model-api.json`；接口继续只返回“是否已保存”。
- 登录密码通过交互终端设置；`private/cloud.env` 只保存登录密码哈希、站点地址与数据目录，不含模型 Key。Git、源码包、Docker 上下文排除该目录。该文件和数据卷备份仍须私密保存。
- 云端和本机的数据彼此独立；没有自动上传本机日程、资料、密码或通知地址。需要迁移时，另行明确迁移范围后停止服务并备份。

## 自有服务器：Docker Compose

在服务器克隆现有私有仓库，并切换到当前默认分支 `codex/initial-import`。使用 Python 3.12 和 Docker Compose；域名需指向这台服务器，开放80/443供 Caddy 申请证书与 HTTPS 访问。云托管平台自带的 HTTPS 域名也可使用，不必购买域名。

在项目根目录操作：

```sh
python3.12 code/cloud_admin.py --url https://你的域名 --output private/cloud.env
cp deploy/cloud.env.example .env
# 编辑 .env，将 DAYNEST_PUBLIC_URL 改成同一个 HTTPS 地址；这里不填模型 Key。
docker compose --env-file .env -f deploy/compose.yml --profile https up -d --build
```

`cloud_admin.py` 只依赖 Python 标准库，不安装 SDK 或模型。密码至少12个字符、输入不回显；生成文件使用独占创建，已有文件不会被覆盖。POSIX 文件权限为0600，Windows 沿用用户目录 ACL。

Compose 的 `daynest_data` 挂载到 `/data`，保存全部数据库、会话、上传后的解析内容、配置与执行记录。`caddy_data` 保存 HTTPS 证书。Web 端口只映射到主机回环地址，公网入口为 Caddy。不要执行 `docker compose down -v`，它会删除数据卷。

升级时停止旧实例、先备份数据卷及 `private/cloud.env`，再构建并启动；保持一个副本。不要把多台机器指向同一个 SQLite 网络共享目录。备份需要单独安排，持久化卷不等于自动备份。进行中的模型请求可能中断，应核对结果后再重试。

## 已有 Python 托管平台或反向代理

```sh
python -m pip install -r requirements-cloud.txt
python code/cloud_admin.py --url https://你的站点 --data-dir /持久化磁盘/daynest --output private/cloud.env
python code/run_cloud.py --env-file private/cloud.env --check
python code/run_cloud.py --env-file private/cloud.env
```

默认网关只监听127.0.0.1:8080，交给同机 HTTPS 反向代理。平台容器使用 `--host 0.0.0.0`；`PORT` 可由平台注入。必须由平台或代理终止 HTTPS，保留真实 Host 和 Origin；不要将内部 HTTP 端口直接暴露公网。`/healthz` 只返回是否就绪，不暴露版本、路径或账号。

也可在平台私密环境变量中配置 `DAYNEST_PUBLIC_URL`、`DAYNEST_DATA_DIR`、`DAYNEST_PASSWORD_HASH`。密码哈希在自己的终端生成并私密填写，不进 GitHub。平台挂载的磁盘必须可由容器非 root 用户 UID 10001 写入。

## Windows 和 macOS

原有 `启动Daynest.cmd` / `.command` 继续启动本机版，完全不需要云端依赖或登录密码。可选云端网关使用支持 Windows、macOS、Linux 的 Waitress；不依赖 Unix socket、fork 或 Gunicorn。

Windows 手动校验示例（在有依赖的 Python 3.12 终端中）：

```powershell
.venv\Scripts\python.exe -m pip install -r requirements-cloud.txt
.venv\Scripts\python.exe code\cloud_admin.py --url https://你的站点 --data-dir C:\DaynestData --output private\cloud.env
.venv\Scripts\python.exe code\run_cloud.py --env-file private\cloud.env --check
```

配置检查不启动服务、不创建数据目录、不联系模型。Docker/Caddy 组合面向云端 Linux 服务器；Win/Mac 浏览器均通过同一个 HTTPS 地址访问云端。

## 登录与验证

云端仅支持单个所有者，不是多用户系统。密码采用带随机盐的 PBKDF2 哈希；会话令牌只存哈希，8小时到期；Cookie 使用 Secure、HttpOnly、SameSite=Strict。退出删除会话，重设密码并重启会撤销旧登录。10分钟内10次失败会暂时限制登录。

登录后仍需原有 CSRF 校验；日程写入、资料归档与同步仍需确认。云端禁止调用本机服务停止接口。数据目录独占锁防止在同一卷上重复运行提醒调度器。

发布前验收：未登录无法读取业务 API；登录后可查询日历、添加待办并确认草案；退出后旧会话不可用；重启后数据保留；设置保存 Key 后读取接口不回显。真实模型、CalDAV、外部通知和公网证书仍需在实际托管环境联调。

GitHub 的 `Cloud image check` 工作流只构建和验证镜像，不发布到镜像仓库、不部署服务，不使用任何个人 API Key。部署目标确定后再接平台自动发布，不能把“构建通过”当作“公网已上线”。

资料：[GitHub Pages 范围](https://docs.github.com/en/pages/getting-started-with-github-pages/what-is-github-pages)、[Waitress 平台支持](https://docs.pylonsproject.org/projects/waitress/en/stable/)、[Docker 环境配置](https://docs.docker.com/compose/how-tos/environment-variables/set-environment-variables/)。

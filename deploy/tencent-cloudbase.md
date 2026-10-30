# Daynest · CloudBase 按需部署

本方案用于已经存在、带 PostgreSQL 的 CloudBase 环境，提供单个所有者使用的按需在线版。**当前文档和代码不代表已经上线**；实际 HTTPS 地址、云端读写、重启恢复和 Linux 镜像仍需按下文验收。不会自动创建付费环境、升级套餐或开启数据库外网。

## 数据如何保存

设置 `DAYNEST_STORAGE=cloudbase` 后，应用保留原有 SQLite SQL 和事务逻辑。每次打开事务时，从固定 HTTPS PostgREST RPC 读取该库的压缩快照，在内存中的 SQLite 执行；有变更的事务提交时，同步将新快照保存到 PostgreSQL，确认成功后才返回。它不是把日历、待办等业务表迁成 PostgreSQL 原生表，也不是定时复制容器文件。

`calendar`、`assistant`、`knowledge`、`automations`、`caldav`、`agent-runs`、`chat-integrations`、`quality`、`cloud-auth` 共 9 个 SQLite 库分别保存快照。`model-api.json`、`notifications.json`、`caldav.json` 也通过远程文档 CAS 保存。`DAYNEST_DATA_DIR` 在此模式下是临时工作目录，不承担持久化；其他工作区临时文件不属于上述持久化数据。

服务端同时使用快照版本 CAS 和全局请求租约，防止旧版本覆盖新版本，并串行保护跨多个存储的业务流程。实例上限设为 1 仍不能替代租约，因为滚动发布时可能出现新旧进程重叠。该机制不将多个 SQLite 库的提交合并成一笔 PostgreSQL 跨库事务。

适用范围是个人小数据工作台。每个 SQLite 库原始快照上限 **32MiB**、压缩后上限 **3MiB**；每份私密 JSON 上限 **64KiB**，模型 API 配置仍保留原有的更严格校验。读取和变更提交传输的是整库快照，数据变大会增加网络、内存、延迟和资源点消耗，不适合作为大规模资料库或 RAG 存储。

远程读写失败不会回退到本机文件或空白数据库。保存结果未确认时，先刷新核对结果，不要重复提交。数据库内快照也不等于独立备份。

## 休眠和提醒

CloudBase 模式不启动后台 APScheduler。只有登录后的有效业务请求才会推进提醒检查，界面会提示当前按需运行的限制。浏览器未使用、实例缩容为 0 或环境停服时，不会准时发送提醒；错过原有发送窗口的提醒不补发，也不会自动重发结果不明的外部通知。

建议使用最小实例数 **0**、最大实例数 **1**。可先尝试 **0.25 核 CPU / 0.5GiB 内存**；这是待实际部署验证的起步配置，不是已经测得的容量保证。首次访问可能需要冷启动。不要为了模拟常驻提醒添加定频唤醒或每分钟查询数据库的任务。

## 费用边界

仅使用现有环境套餐支持的云托管和 PostgreSQL HTTPS Data API，不挂 CFS、不启用数据库原生外网、不购买域名或额外资源。体验版有每月资源点与有效期限制，计算、数据库、存储、日志和流量会共同消耗额度；它不是无限免费或可无限持续运行的服务。保持体验版，不开启付费升级、资源包或超限付费；额度耗尽时接受停服。

当前额度、有效期及续期要求以目标环境的「套餐用量」页面为准。参见 [资源点与体验版规则](https://cloud.tencent.com/document/product/876/127357)、[PG 共享实例计量](https://cloud.tencent.com/document/product/876/120713)。

## 准备 PostgreSQL 和访问身份

1. 在目标环境的 PostgreSQL **SQL 编辑器**中，一次应用仓库内的 [cloudbase.sql](cloudbase.sql)。脚本创建专用 `daynest_private` schema、快照及租约表，并创建固定的 `public.daynest_*` RPC。脚本不包含环境 ID 或任何凭据。不要用含任意 SQL 参数的通用 RPC 替代它。
2. **不能假定普通 CloudRun 容器自动获得可用的 CloudBase 凭据。** 未手动配置 `DAYNEST_CLOUDBASE_KEY` 时，代码会尝试读取当前进程的 `TENCENTCLOUD_SECRETID`、`TENCENTCLOUD_SECRETKEY`、`TENCENTCLOUD_SESSIONTOKEN`；这是待目标运行时验证的候选路径，三项确实注入且实际权限验证通过时才可用。缺失或无效就停止启动，不创建任何 Key，也不查询本机 CLI 凭据或文件。
3. 候选临时身份路径每次获取 Bearer 都重新读取三项变量，用 TC3 签名请求 `/auth/v1/token/clientCredential`，再以返回的 Bearer 调用固定 RPC。缓存有效期取 `expires_in - 60 秒` 与 600 秒中的较短值；凭据变化或缺失立即清除缓存，不跟随重定向。
4. 无论采用哪种身份，部署启动时都必须用随机的新 owner 调用只读 `daynest_lease_check`，返回**精确的 JSON `false`**才继续。这既检查 RPC 已安装，也检查当前身份通过原有 `service_role` 门禁。换令牌失败、过期或角色不符时停止启动，不能放宽 SQL 门禁或用本机数据替代。

官方资料存在适用范围分歧：[通用 SDK 初始化文档](https://docs.cloudbase.net/api-reference/webv3/initialization)描述云托管自动读取环境鉴权，但较具体的[运行时凭据说明](https://github.com/TencentCloudBase/CloudBase-AI-Toolkit/blob/main/config/.claude/skills/cloud-functions/references/http-function-credentials.md)明确覆盖函数型和容器型 CloudRun，指出不能依赖容器自动拥有可用凭据；[环境变量专页](https://docs.cloudbase.net/run/deploy/configuring/environment/envs)也没有默认注入三项临时变量的承诺。不能据通用说明保证当前创建的容器可用，也没有足够资料确定新旧版本身份行为的切换时间。

[3.5.2 更新记录](https://docs.cloudbase.net/api-reference/webv3/changelog)确认 RDB 支持 V3 签名换取 access_token；Python 实现与公开官方签名库的假凭据向量离线核对过。这只验证请求算法，不能证明目标容器已获得身份、平台会持续刷新凭据或该 Token 实际映射为 PG `service_role`。以上述真实启动检查为准，不回显 HTTP headers、环境变量或令牌。

固定 RPC 内部还会检查角色和租约；不能仅依赖 `GRANT EXECUTE`，因为 CloudBase 当前对 RPC 的网关权限检查存在官方说明的边界。参见 [PG 鉴权](https://docs.cloudbase.net/authentication-v2/auth/auth-pg)、[RPC 权限说明](https://docs.cloudbase.net/database/postgresql/rpc)。

兼容的 `DAYNEST_CLOUDBASE_KEY` 模式仅适用于用户另行明确授权的后端 API Key。其 `api_key` 类型对应 `service_role`，拥有整个环境的数据流管理权限。创建有效期 180 天的此类 Key 也需要针对该权限与期限的明确授权；等待回复期间不能创建。不要因临时身份失败而自动创建它，也不要选用 Publishable Key。平台部署表单的「API Key 设置」复选框用于选择和注入 API Key，不是临时身份开关；未经授权不启用。确需创建时入口为「环境配置 → API Key 配置」（部分界面称「环境管理 → API 密钥」）。它只能私密注入后端运行变量，不能进入前端、源码、GitHub / Actions Secrets、构建参数、镜像、日志或源码 ZIP；它与 Daynest 内配置的模型 Key 是两种凭据。参见 [API Key 权限](https://cloud.tencent.com/document/api/876/129835)。

## 部署容器与运行变量

使用现有项目的 Dockerfile 容器部署，构建上下文为项目根目录，Dockerfile 为 `Dockerfile`，监听端口为 **8080**。镜像命令已监听 `0.0.0.0` 并读取平台 `PORT`。优先使用该服务自己的 HTTPS 默认域名，无需部署 Compose / Caddy，也不占用同环境其他应用的根路由。上传前检查源码包中没有 `private/`、`.env*`、`output/`、日志或本机运行数据；不要直接压缩整个本机工作区。参见 [官方部署方式](https://docs.cloudbase.net/run/deploy/deploy/introduce)。

默认域名是开发测试入口，首次导航会显示 CloudBase 访问提示；不能当作无访问限制的正式自定义域名。[默认域名说明](https://docs.cloudbase.net/service/alias)。容器 HTTP 请求默认限时约60秒，应用内部240秒等待并不等于公网支持240秒；长模型任务可能中断，需要核对当前套餐及控制台是否允许调整请求超时。官方容器教程提及「服务设置 → 高级配置 → 请求超时」，实际有无此项及额度以控制台为准，不能猜 API 参数或自动升级套餐。[请求限制](https://docs.cloudbase.net/run/limitation)、[容器 Agent 配置](https://docs.cloudbase.net/recipes/deploy-mastra-agent-to-cloudbase-run)。

在**运行环境变量**中设置以下值，不要当作构建参数填写：

| 变量 | 填写方式 |
| --- | --- |
| `DAYNEST_STORAGE` | `cloudbase` |
| `DAYNEST_CLOUDBASE_ENV` | 当前已有环境的完整环境 ID |
| `DAYNEST_CLOUDBASE_KEY` | 未配置时仅尝试临时身份候选；缺少有效注入或启动探针失败就停止。仅获明确授权后配置后端 API Key |
| `DAYNEST_PUBLIC_URL` | 实际使用的 HTTPS origin，仅协议和域名，不含路径、查询或片段 |
| `DAYNEST_PASSWORD_HASH` | 下文交互生成的 PBKDF2 登录哈希，私密保存 |
| `DAYNEST_DATA_DIR` | `/data`，容器 UID 10001 可写的临时工作目录 |
| `PORT` | 如平台需要手动填写，设置为 `8080`，与监听端口一致 |

必须使用实际分配的访问域名，不能把文档示例域名当作已经存在的站点。Host / Origin 必须与 `DAYNEST_PUBLIC_URL` 一致，才能通过登录、Cookie 和 CSRF 校验。服务域名确定后，补齐上述变量再运行版本。参见 [服务设置](https://docs.cloudbase.net/run/deploy/service-setting)。

登录哈希在本机已有 Python 3.12 的交互终端中生成，密码不通过命令行或聊天传递。将示例域名替换为实际 HTTPS origin。macOS / Linux：

```sh
python3.12 code/cloud_admin.py --url https://daynest.example.com --data-dir /data --output private/cloudbase-login.env
```

Windows（使用项目 Python 3.12 环境）：

```powershell
.venv\Scripts\python.exe code\cloud_admin.py --url https://daynest.example.com --data-dir C:\DaynestCloudConfig --output private\cloudbase-login.env
```

只将生成文件中的 `DAYNEST_PASSWORD_HASH` 值填入平台运行变量，去掉文件格式使用的外层单引号；其中的 `$` 字符原样保留。平台 `DAYNEST_DATA_DIR` 仍填 `/data`，不要复制 Windows 路径。生成文件留在自己的私密目录，不上传 GitHub、镜像或部署包。

如需本地检查环境变量格式，可执行 `python code/run_cloud.py --check`。该命令不启动服务、不连接 PostgreSQL、不读取临时身份、不验证权限或 RPC 是否已经安装，不能代替云端启动检查和验收。

## 首次登录与验收

首次云端数据为空，不读取、迁移或复制电脑上的日程、资料、会话及 Key。部署成功后，通过实际 HTTPS 入口登录，再在「设置 → 模型 API」填写模型地址、Key 和模型名称。Key 经登录后的 HTTPS 设置接口单独保存到该云环境的私密文档中，不经过源码、构建或 GitHub。

上线验收至少包括：

- 未登录不能读取业务 API；登录、退出及 CSRF 校验正常。
- 添加一条测试待办并确认草案；重建 / 重启实例后，登录状态及业务数据按现有规则恢复。
- 保存模型配置后，公开查询只显示是否已保存 Key，不回显凭据；没有模型配置时仍能使用基础功能。
- 远程不可用或发生并发冲突时返回明确错误，不显示本机残留或用空库覆盖数据。
- 实例允许缩容至 0，界面显示按需提醒限制；没有后台调度器或额外的定频唤醒任务。
- 查看实际内存、冷启动耗时、资源点用量和日志，再判断起步规格是否满足个人使用。

GitHub [cloud-build.yml](../.github/workflows/cloud-build.yml) 在 Linux 镜像内运行云端登录、快照、配置 CAS、临时身份和网关测试，并检查镜像排除私密文件。测试使用假的凭据和存储，不部署服务、不联系真实模型或 CloudBase。应核对对应代码提交的真实工作流结果，再在目标云环境验证读写与重启；本机没有运行 Docker 不应记作镜像验证通过。

需要持续运行并准点提醒时，使用 [自有服务器部署](README.md)；该选择可能需要已有服务器资源。本文不自动购买或升级任何资源。

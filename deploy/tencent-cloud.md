# Daynest · 腾讯云操作指南

适用于完整在线版：GitHub 保存私有源码，腾讯云服务器运行 Docker，Caddy 提供 HTTPS，日程、资料与配置保存在持久化数据卷。没有替你创建资源或购买套餐。

## 先确认是否已有资源

1. 登录 [腾讯云控制台](https://console.cloud.tencent.com/)。
2. 打开 [轻量应用服务器](https://console.cloud.tencent.com/lighthouse)，检查服务器列表；列表为空时，切换顶部地域确认其他地域。
3. 再检查 [云服务器 CVM](https://console.cloud.tencent.com/cvm) 的实例列表与地域。
4. 控制台搜索“域名注册”，进入“我的域名”查看域名。云解析 DNS 中有解析记录，不一定代表域名在腾讯云注册；其他注册商的域名也可以使用。
5. 如有服务器，记录类型、地域、系统、CPU/内存和运行状态。没有资源时先确认预算与免费额度，再决定是否创建；本项目不假定腾讯云长期免费。

这些信息不含账号密码、Tencent SecretKey、模型 API Key 或私钥，不需要把任何密钥发到聊天或 GitHub。

## 创建或选择服务器

已有实例先按现有系统评估。新建时，个人自用可考虑轻量应用服务器，选择 x86_64 架构的 Docker CE 应用镜像。腾讯云官方 Docker CE 示例基于 Ubuntu Server 24.04 LTS，实际镜像以创建页为准。[官方教程](https://cloud.tencent.com/document/practice/1207/60423)

本项目建议起步配置为2核、4GB内存、40GB系统盘，方便安装文档解析依赖；这是工程建议，不是已经测得的最低要求。无需 GPU 或本地大模型。服务器、域名和超出额度的资源可能收费，购买及续费价格以账号订单为准。

按腾讯云指引，中国内地地域通过域名提供网站服务需先完成 ICP 备案；域名解析到中国香港等非中国内地地域的服务器不需要该项备案。地域也会影响访问模型 API、GitHub 和镜像仓库的连通性，应按实际资源选择。[腾讯云备案说明](https://cloud.tencent.com/document/api/243/19630)

## 域名和端口

将自己的子域名（例如 `daynest.example.com`）添加一条 A 记录，记录值为服务器公网 IPv4 地址。可用腾讯云云解析 DNS，也可使用已有 DNS 服务商；等解析生效后再申请 HTTPS。

轻量实例在“防火墙”设置，CVM 在关联安全组设置入站规则：

| TCP 端口 | 用途 | 来源 |
| --- | --- | --- |
| 22 | 自己通过 SSH 管理服务器 | 自己的公网 IP；控制台登录按平台要求配置 |
| 80 | HTTPS 证书校验、HTTP 跳转 | `0.0.0.0/0` |
| 443 | Daynest HTTPS | `0.0.0.0/0` |

本方案不对公网开放8080、8787或数据库端口。若系统防火墙已启用，也需匹配放行80/443并保留现有管理入口。[腾讯云防火墙操作](https://cloud.tencent.com/document/product/1207/44577)

## 登录服务器与准备环境

在实例列表点“登录”，使用腾讯云的浏览器 SSH 登录。以下命令在服务器终端执行，与你的 Win/Mac 电脑系统无关。

```sh
docker --version
docker compose version
python3 --version
git --version
```

使用 Ubuntu 24.04 / Docker CE 镜像时，缺少 Python 或 Git 可通过该系统的包管理器安装；其他系统先匹配对应步骤。Python 建议3.12，Docker 需支持 Compose 插件。Docker CE 镜像环境参见前述官方教程。

## 从私有 GitHub 仓库拉取源码

推荐只读 Deploy Key。先在服务器生成一对部署专用 SSH 密钥：

```sh
ssh-keygen -t ed25519 -C daynest-tencent -f ~/.ssh/daynest_deploy
cat ~/.ssh/daynest_deploy.pub
```

只复制 `.pub` 公钥，添加到 GitHub 仓库 **Settings → Deploy keys → Add deploy key**，保持只读，不勾选写权限。私钥保留在服务器，模型 API Key 不参与这个流程。[GitHub 指引](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys)

```sh
GIT_SSH_COMMAND='ssh -i ~/.ssh/daynest_deploy -o IdentitiesOnly=yes' git clone --branch codex/initial-import git@github.com:zhang1845/personal-agent.git Daynest
cd Daynest
git config core.sshCommand 'ssh -i ~/.ssh/daynest_deploy -o IdentitiesOnly=yes'
```

## 配置登录并启动

把下列示例域名改成你自己的、已解析并符合备案要求的域名：

```sh
python3 code/cloud_admin.py --url https://daynest.example.com --output private/cloud.env
cp deploy/cloud.env.example .env
nano .env
```

终端会要求输入两次 Daynest 登录密码，至少12个字符、不回显。`.env` 里仅填写相同的 `DAYNEST_PUBLIC_URL=https://daynest.example.com`；模型 API Key 不填在这两个文件中。

```sh
sudo docker compose --env-file .env -f deploy/compose.yml --profile https up -d --build
sudo docker compose --env-file .env -f deploy/compose.yml --profile https ps
```

Caddy 会尝试申请 HTTPS 证书。申请需要域名指向此服务器且80/443可访问，不能在域名和网络尚未就绪时保证成功。

打开 `https://daynest.example.com`，先登录，再进入“设置 → 模型 API”填写 API 地址、Key 和模型名称。Key 只保存在此服务器的数据卷，不上传 GitHub，不进入镜像和 Actions Secrets。首次云端数据为空，不会自动复制电脑上的 Key 或资料。

## 验收与维护

- 未登录打开业务页面应跳转登录；业务 API 不应返回个人数据。
- 登录后添加一条自己的测试待办，退出再登录确认保留。
- 模型配置保存后再测试连接和工具调用；未配置时仍可使用基础指令。
- 重建容器后核对日程和待办；持久化数据卷及 `private/cloud.env` 需要独立备份。
- 不执行带 `-v` 的 `docker compose down`；服务器销毁、磁盘丢失或过期仍可能造成数据丢失，数据卷不等于异地备份。

本项目当前尚未替你创建腾讯云资源，也没有得到实际域名和服务器地址。公网证书、模型连通性与腾讯云实机部署须在上述资源确定后验收。完整限制和跨平台说明见 [通用部署文档](README.md)。

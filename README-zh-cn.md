# MarzbanX

MarzbanX 是基于 [Gozargah/Marzban](https://github.com/Gozargah/Marzban) 的 fork，当前重点是把 node 管理从“手动 SSH 改配置”推进到“主控面板托管协议选择、自动生成 inbound/host、自动安装 Rust node、自动选择 Xray 或 sing-box”。

> 兼容性说明：这个 fork 目前仍保留不少上游运行时名称，例如 `marzban`、`marzban-cli`、`/var/lib/marzban` 和已有 service 名称，避免升级和部署路径被一次性打断。对外项目名改为 MarzbanX。

## 语言

[English](./README.md) / [简体中文](./README-zh-cn.md) / [فارسی](./README-fa.md) / [Русский](./README-ru.md)

## 项目链接

- 当前 fork：[Ryanisgood/MarzbanX](https://github.com/Ryanisgood/MarzbanX)
- 原版上游：[Gozargah/Marzban](https://github.com/Gozargah/Marzban)
- Rust node：[MarzbanX-node](../MarzbanX-node)
- Node provisioning 文档：[docs/node-provisioning.md](./docs/node-provisioning.md)
- CLI 文档：[cli/README.md](./cli/README.md)

## MarzbanX 改了什么

MarzbanX 保留 Marzban 的主控、REST API、Dashboard、订阅模板、用户管理、Telegram Bot 和 CLI，然后把 node 侧体验改成由面板托管协议选择，而不是让用户到 node 机器上手动维护 `INBOUNDS`。

这个 fork 最近的核心修改包括：

- node 支持 `active_inbounds`，主控明确知道每个 node 正在跑哪些 inbound；
- inbound 行增加 `owner_node_id`，panel-managed node 只能选择属于自己的 inbound；
- Add Node 向导会自动创建 generated inbounds、hosts、panel-mode node 和一次性安装命令；
- core 策略确定化：包含 sing-box-only 协议时使用 sing-box，否则使用 Xray；
- Rust node 支持 controller-managed inbounds；
- node 上报运行状态：node 版本、已安装 core、当前 core、内存、监听端口、配置端口、最近重启时间；
- HY2/Hysteria2 和 AnyTLS 用户变更会重建 sing-box 配置，并尽量只重启受影响 node；
- provisioning 生命周期增加校验、回滚、token 重试、端口冲突检查。

## 功能

- Web Dashboard 和 REST API，用于管理用户、管理员、节点、host、订阅。
- 通过 Rust [MarzbanX-node](../MarzbanX-node) runtime 做多节点部署。
- Panel-managed node：主控把选中的 inbound tags 下发给 node，新 node 不再需要手动写 `INBOUNDS`。
- Add Node 向导自动生成安装命令：

```bash
curl -fsSL https://controller.example.com/api/node/install.sh | sudo bash -s -- --token xxx
```

- 当前 provisioning 模板：HY2/Hysteria2、AnyTLS、VLESS TCP REALITY、Shadowsocks TCP。
- Core 策略可视化：expected core、actual core、原因、Xray API 是否可用、是否需要重启。
- Node 诊断：Xray/sing-box 安装版本、本地 socket、配置端口、内存、node 版本、重启时间。
- 流量限制、到期限制、周期流量重置、多用户、多协议账号。
- 输出 V2Ray 兼容、sing-box、Clash、Clash Meta 等订阅格式。
- 继承上游 Marzban 的 TLS、REALITY、Telegram Bot、Webhook、Backup、`marzban-cli` 等能力。

## Add Node 流程

MarzbanX 目标流程：

1. 打开 Dashboard 的 node 弹窗。
2. 使用 Add Node，而不是 advanced/manual form。
3. 填写 node 名称、公网地址、管理端口、协议和 public inbound ports。
4. 主控自动创建 generated inbounds、hosts、panel-managed node 和短期 token。
5. 在新机器执行面板生成的一条命令。
6. Rust node 安装或使用所需 core，启动 `marzban-node.service`，后续由主控下发 active inbound tags。

### Node-Owned Inbounds

MarzbanX 现在不再把“全局 inbound 复用”作为普通 node 管理方式。Add Node 会给新 node 创建专属 inbound，这些 inbound 通过 `owner_node_id` 归属到该 node。Dashboard 只允许这个 node 选择自己的 inbound，运行时启动/重启 node 前也会做同样校验。

说人话就是：

- 用户仍然按协议维护，比如给用户启用 VLESS、HY2、SS；
- node 能跑什么协议，取决于这个 node 自己拥有的 inbound；
- 一个 node 不能再选择另一个 node 的 inbound；
- 没有 `owner_node_id` 的 inbound 视为旧数据/手动模式迁移数据，不再作为普通 Add Node 流程使用；
- Rust node 的 `INBOUNDS` 环境变量只保留给 legacy/manual node。Panel-managed node 由主控下发 active inbound tags。

升级旧数据时，形如 `node-{id}-{protocol}-{port}` 的 generated tags 可以被迁移为该 node 的 owned inbound。如果升级后 Dashboard 显示某个已选 inbound 无效，优先移除这个选择并通过 Add Node 重新创建协议；只有确认它确实是该 node 专用 inbound 时，才手动补 `owner_node_id`。

生产环境使用向导前建议配置：

```env
MARZBAN_NODE_BINARY_URL=https://controller.example.com/downloads/marzban-node
SING_BOX_INSTALL_SCRIPT_URL=https://controller.example.com/downloads/install-sing-box.sh
XRAY_INSTALL_SCRIPT_URL=https://github.com/XTLS/Xray-install/raw/main/install-release.sh
```

`MARZBAN_NODE_BINARY_URL` 应指向给目标 Linux 架构构建的 Rust `MarzbanX-node` binary。`SING_BOX_INSTALL_SCRIPT_URL` 用于 HY2、AnyTLS 或任何 sing-box 组合，除非 node 镜像里已经预装 sing-box。

## Core 策略

每个 node 同一时间只启动一种 core：

- active inbounds 包含 HY2/Hysteria2 或 AnyTLS 时，expected core 是 `sing-box`；
- 其他 Xray-compatible 选择使用 `xray`；
- 只有 Xray-selected config 会注入 Xray API；
- sing-box node 不暴露 Xray API，因此 usage collection 会跳过 sing-box node 的 Xray usage API。

Dashboard 会直接显示：

- 当前 core；
- 预期 core；
- 原因，例如 `INBOUNDS contains sing-box-only protocol`；
- Xray API 是否可用；
- 是否需要重启；
- active inbound 明细、public ports、用户数。

## 协议切换

Panel-managed node 可以在 Dashboard 修改 active inbound selection。主控会校验：

- 目标 inbound tags 是否存在；
- 需要 host 时 host 是否存在；
- 同一 bind/transport 下端口是否冲突；
- 协议组合是否被目标 core 支持；
- node 是否上报了所需 core 的安装状态。

VMess/Trojan provisioning 和更完整的协议切换向导仍属于 roadmap。Rust node 已经有部分 sing-box translation 能力，但当前 Dashboard provisioning 只暴露上面列出的模板。

## HY2 和 AnyTLS 用户变更

HY2 和 AnyTLS 在这个 fork 中属于 config-reload 协议。创建、修改、删除或迁移这类用户时，会重建 sing-box 配置并重启受影响 node。Dashboard 会提示已有连接可能短暂中断。

## 本地开发

```bash
git clone https://github.com/Ryanisgood/MarzbanX.git
cd MarzbanX
python3 -m pip install -r requirements.txt
alembic upgrade head
cp .env.example .env
python3 main.py
```

创建 sudo admin：

```bash
marzban cli admin create --sudo
```

Dashboard 默认地址：

```text
http://localhost:8000/dashboard/
```

Dashboard 开发：

```bash
cd app/dashboard
npm ci
npm run dev
```

## 配置

上游 Marzban 的大部分配置仍然适用。MarzbanX 里和 node provisioning 相关的重点配置：

| 变量 | 说明 |
| --- | --- |
| `MARZBAN_NODE_BINARY_URL` | 一键安装脚本下载 Rust node binary 的 URL。 |
| `SING_BOX_INSTALL_SCRIPT_URL` | sing-box node 的安装脚本 URL。HY2/AnyTLS provisioning 通常需要它。 |
| `XRAY_INSTALL_SCRIPT_URL` | Xray node 的安装脚本 URL，默认使用上游 Xray installer。 |
| `XRAY_JSON` | 主控 core config 文件，generated inbounds 会写入这里。 |
| `XRAY_EXECUTABLE_PATH` | 主控本机 Xray binary 路径。 |
| `XRAY_ASSETS_PATH` | 主控本机 Xray assets 路径。 |
| `DOCS` | 设置为 `True` 后开放 `/docs` 和 `/redoc`。 |

完整配置请看 [.env.example](./.env.example) 和 [config.py](./config.py)。

## API

设置 `DOCS=True` 后打开：

- `/docs`
- `/redoc`

Node provisioning 相关接口：

- `POST /api/node/provision`
- `GET /api/node/install.sh`
- `POST /api/node/provision/redeem`

## 验证

和当前 node 改动最相关的检查：

```bash
python -m pytest tests/test_node_provisioning.py tests/test_node_active_inbounds.py tests/test_hysteria_support.py -q
bash build_dashboard.sh
XRAY_EXECUTABLE_PATH=/bin/echo alembic heads
```

## Roadmap

- 在不破坏升级路径的前提下，把剩余 runtime/package/image 名称从 Marzban 逐步改为 MarzbanX。
- 扩展 Add Node 向导到 VMess、Trojan。
- 协议切换前增加更完整的防火墙/可达性检查。
- 增加 provisioned node token 重新签发或轮换流程。
- 用最终仓库名发布 MarzbanX 和 MarzbanX-node release artifacts。

## Attribution

MarzbanX fork 自 [Gozargah/Marzban](https://github.com/Gozargah/Marzban)。上游项目、贡献者和 AGPL-3.0 license 仍然是这个 fork 的基础。

原版 README 中的打赏地址已从本 fork README 移除。

## License

基于 [AGPL-3.0](./LICENSE) 发布。

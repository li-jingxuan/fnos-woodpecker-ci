# Woodpecker CI 飞牛应用包

把 Woodpecker Server、一个 Docker Agent 和 Nginx 网关代理封装为由 fnOS 应用中心管理的 FPK。应用名为 `woodpecker`，版本 `0.1.1`，目标架构为 x86_64；页面前缀固定为 `/app/woodpecker`。安装使用飞牛应用中心向导，CI 操作使用 Woodpecker 自身页面。

## 交付范围与验证状态

源码包含 Manifest、资源与权限声明、桌面入口、安装/配置/卸载向导、生命周期脚本、配置生成与代理规则，以及离线校验和构建脚本。Server 和 Agent 固定为 `v3.20.1`，代理固定为 `nginx:1.28-alpine`；FPK 不内置容器镜像。

`scripts/validate-package` 实现静态契约检查，`scripts/build-fpk` 实现临时 staging 打包与 SHA-256 产物输出。静态检查通过、FPK 生成成功、NAS 安装成功和完整 CI 成功是不同结果。静态检查不能证明 Docker 权限、飞牛调用时序、FN Connect、OAuth 或 Webhook 可用。

**当前交付已通过真实 OAuth 与手动构建验证，自动 Webhook 链路仍待完成。** 本机 32 项测试及静态检查通过；NAS 完成真实 FPK 安装、27 项基础检查、冷备和离线恢复，并使用真实 GitHub 账号完成授权回跳。专用私有仓库的两次手动构建成功，故意失败构建返回退出码 17，GitHub 分别收到成功和失败状态；真实任务的 2 CPU / 1.5 GiB 限额已在内核 cgroup 核对。当前 FN Connect Webhook 投递返回 HTML，尚未证明外部 push 自动触发；Docker Hub 不可达时，测试仓库显式使用官方 Quay 克隆镜像。详见 [NAS 验证报告](docs/20261104_NAS部署验证报告.md)，按[技术方案](docs/20261103_Woodpecker飞牛应用部署技术方案.md)的 A01–A22 记录通过范围和缺项。

目标平台普通停止/启动复用容器，**配置向导现已阻止修改**，避免文件更新而运行实例仍使用旧值。本次在冷备后人工执行本项目的 Compose 容器重建，再交由应用中心启动，已验证真实 OAuth 配置生效；该维护过程尚未接入配置向导或完成真实失败回退验收。`install-fpk` 遇到已安装应用直接返回；`install-local` 在测试中执行卸载重装，且可能忽略卸载回调错误，不作为可靠的升级入口。生产使用前仍需验证平台支持的升级和回退流程。

实际项目 `li-jingxuan/liuxianmao` 的 `openchamber/pindou` 分支已通过完整流水线验证：后端 261 项测试、前端 172 项测试和生产构建全部成功。支持通过 `BUILD_TARGET=api/web/all` 单独选择任务；PostgreSQL 仅用官方镜像提供临时后端测试数据库。自定义源码下载已改为普通步骤，最新第 10 次任务无解析警告。详见[完整构建报告](docs/20261104_Pindou流水线验证.md)及[单独构建与警告修复记录](docs/20261104_Pindou单独构建验证.md)。最新 OAuth 登录复测再次遇到 NAS 到 GitHub 的连接超时；FN Connect 公网 Webhook 也仍未打通，本机通知投递成功不能替代公网自动触发验收。

## 源码与离线检查

包源目录为 `packaging/woodpecker/`：`manifest` 声明应用，`config/{resource,privilege}` 声明 Docker 项目及包用户，`wizard/{install,config,uninstall}` 为向导，`cmd/` 为应用生命周期，`app/docker/docker-compose.yaml` 为容器定义，`app/scripts/runtime.py` 处理运行配置，`app/ui/config` 为飞牛入口。

在仓库根目录运行：

```bash
python3 scripts/validate-package
python3 -B -m unittest discover -s tests -v
# 也可校验独立的包目录
python3 scripts/validate-package /absolute/path/to/woodpecker
```

校验器仅使用 Python 3 标准库，并调用本机 `bash -n` 检查 Shell 语法，不要求 Docker daemon。它检查必备文件及执行位、Manifest 必要字段、JSON 语法与重复键、资源和入口契约、固定前缀与镜像版本、64/256 像素 PNG 的 IHDR、常见明文凭据和私钥模式。Python 源码用内存中的 `compile()` 检查，不生成项目 `__pycache__`。凭据扫描属于静态筛查，发布前仍须确认打包目录只含模板和源码。

## 构建 FPK

准备 Bash、Python 3 和从 fnOS 官方 SDK 获得的本机 `fnpack`。可使用 NAS 上已安装的工具；脚本不会在线下载、安装或执行外部工具。`fnpack` 版本和真实打包结果应记入部署报告。

```bash
scripts/build-fpk
# fnpack 不在 PATH 中时，指定已有的可执行文件
FN_PACK_BIN=/absolute/path/to/fnpack scripts/build-fpk
```

构建脚本先校验包，再复制到临时 staging，在该临时目录执行 `fnpack build --directory <绝对包路径>`。打包工具在当前目录或包目录输出 FPK 都不会污染源码目录；退出时清理 staging。成功后生成：

```text
dist/woodpecker-0.1.1.fpk
dist/woodpecker-0.1.1.fpk.sha256
```

`.sha256` 使用标准的「摘要、两个空格、文件名」格式。可在 `dist` 内使用 `sha256sum -c woodpecker-0.1.1.fpk.sha256`（Linux），或 `shasum -a 256 -c woodpecker-0.1.1.fpk.sha256`（macOS）校验。构建失败时不要把旧的 `dist/` 文件当作本次成功产物。

## 安装前准备

NAS 需要可工作的 Docker、支持 `env_file` 的 `format: raw` 的 Compose 调用链、Python 3，以及到 GitHub 和容器镜像仓库的出站网络。生命周期以 root 准备本应用目录及 Socket 集成，随后使用 `runuser` 降为 `docker-woodpecker`；不授予包用户 Docker 组权限。实际包用户的主组由平台决定，不假设与用户名相同。网关组从宿主 `www-data` 动态读取，Socket 为 `root:<网关组> 0660`，不写死数字 GID。网关代码挂载只读，单独挂载 `target/run` 供创建 Socket。

在应用中心手动安装 FPK，使用飞牛提供的安装位置选择数据卷。建议选容量充足的第二存储卷，应用所在卷与 Docker 数据所在卷各预留至少 20 GiB；实际存储位置由 `TRIM_*` 平台变量提供，包内不写死卷路径。镜像、克隆缓存和构建容器占用的空间还会落在 Docker 数据卷。

初始资源预算为 Server 512 MiB / 0.5 CPU、Agent 256 MiB / 0.5 CPU、网关 64 MiB / 0.25 CPU；单个构建容器目标上限为 1536 MiB / 2 CPU，并发 workflow 固定为 1。Agent 的资源上限不包含它创建的构建容器，单条流水线的多个服务/步骤也可能叠加资源。实际生效值必须检查真实构建容器；普通 Docker bridge 不提供 NAS 内网隔离。

Agent 通过宿主 Docker socket 运行任务，该权限接近宿主管理权限。首版只启用明确授权的自有仓库，默认不开放陌生来源 PR、特权构建和任意宿主挂载。不要开放 Docker TCP API，也不要用 `chmod 777` 修复权限。

## GitHub OAuth 与安装向导

1. 确定浏览器实际使用的稳定 HTTPS origin，例如 `https://nas.example.com`。该域名必须在内外网按预期可达，并且是完成飞牛登录后访问应用的同一规范域名。示例域名只用于说明。
2. 登录 GitHub，在 **Settings → Developer settings → OAuth Apps → New OAuth App** 创建 **OAuth App**。Homepage URL 填写完整页面基址 `https://nas.example.com/app/woodpecker`；Authorization callback URL 必须为 `<WOODPECKER_HOST>/authorize`，即 `https://nas.example.com/app/woodpecker/authorize`。
3. 获取 Client ID，生成 Client Secret。将它们直接填入安装向导；不要放进仓库、命令行参数、截图或报告。Client Secret 应当只在受限配置文件中保存。
4. 向导填写页面 origin、单个 GitHub 管理员用户名、Client ID 和 Client Secret，以及适用的 Webhook 地址配置。origin 填协议和主机名（可含端口），不填写 `/app/woodpecker`、查询参数、fragment 或 URL 用户密码；程序统一追加固定前缀。GitHub 管理员用户名与飞牛账号分别配置。

`WOODPECKER_HOST` 最终为无末尾斜杠的完整页面基址，正常首页是 `/app/woodpecker/`。Agent 共享密钥由安装流程随机生成，不需要在 GitHub 创建。配置模块支持空白 OAuth Secret 保留旧值，但当前平台配置向导被阻止；替换或轮换必须先验证容器重建入口。飞牛登录和 GitHub 登录是两套身份验证，桌面入口可见不代表已经获得仓库权限。

本包以 GitHub 作为仓库平台，Woodpecker 使用 GitHub OAuth 确认用户身份并取得其仓库权限；飞牛账号仅控制进入 NAS，不能替代这一步。登录卡在 Loading 时，还需检查 NAS 到 `github.com/login/oauth/access_token` 的出站连接：浏览器能打开 GitHub、`api.github.com` 可达或服务 healthcheck 正常，都不能证明 Server 能完成令牌交换。排查日志时不要输出 OAuth code、令牌或 Secret。

## Webhook 接入

GitHub 服务器发起 Webhook 时没有 NAS 登录 Cookie。FN Connect 页面能打开不足以证明回调可达；先用外部无 NAS 会话请求测试。若被飞牛登录或访问码页面拦截，使用已有 HTTPS 反向代理或稳定的具名隧道提供独立回调入口。包不自动部署隧道，也不修改现有代理。

独立入口只转发 **POST `/app/woodpecker/api/hook`**，其他路径拒绝；宿主进程可连接本机 `127.0.0.1:18081`，该端口需要先确认无冲突。隧道在容器内运行时，其 `127.0.0.1` 指向容器自身，需要按实际网络拓扑配置。不要把完整后台、内部 gRPC 或 Docker API 暴露出去。

页面和 Webhook 必须使用相同的 `/app/woodpecker` 前缀。独立 Webhook origin 生成 `WOODPECKER_EXPERT_WEBHOOK_HOST`；保留空值时使用页面基址，并不保证 FN Connect 能接收 GitHub 投递。仓库在 Woodpecker 中启用后，由 Woodpecker 创建和管理 Webhook；完整地址形如 `<Webhook 基址>/api/hook?access_token=<自动生成令牌>`，不要手工编造令牌。

`v3.20.1` 校验 hook token（JWT）及其仓库关联，**不等于验证 GitHub 请求体的 `X-Hub-Signature-256` HMAC**。入口必须原样保留 token 查询参数、事件 Header 和原始 body，不跳转或自动重试 POST。完整 Webhook URL 是凭据，代理、隧道、CDN 和诊断输出均不能记录 query；泄漏后需要轮换仓库 hook secret/token 并验证旧令牌失效。

改动页面地址后，应同步修改 GitHub OAuth App 的 callback；改动 Webhook 地址后，要重新同步已启用仓库的 GitHub Webhook。重启不会自动保证外部配置已经迁移。

## 启停、配置与升级限制

使用飞牛应用中心管理常驻 Compose 项目。生命周期脚本不自行启动第二套 `compose up/down`。默认 `restart: "no"` 把恢复控制交给应用中心；Docker healthcheck 的 unhealthy 本身不会自动重启。NAS 开机恢复、停止后保持停止、容器故障恢复都需有实测结果。

维护前先在 Woodpecker 中停止接收新任务并处理队列，等待运行任务结束；最多等待 10 分钟后应由操作者选择继续等待或明确取消。若尚未验证自动排空通道，请采用人工维护流程。停止 Agent 不保证其已创建的任务容器立即停止，停止后要确认本应用任务容器与网络已经回收，不能仅凭应用图标判断。

配置生成使用受限文件和完整配置代次，Compose 包含配置代次 label。实测普通应用启动只复用已有容器，因此 label 也不足以促使平台重建。当前配置回调明确失败并保留旧代次；不要绕过回调直接切换 `current`。Server 和 Agent 的共享密钥必须协调轮换。

升级前记录旧 FPK、镜像版本/digest、配置代次与数据库版本，排空任务、停止写入，并导出一致性备份。数据位于 `${TRIM_PKGVAR}/server` 与 `${TRIM_PKGVAR}/agent`，私密配置位于 `${TRIM_PKGETC}`；备份需覆盖两类内容并保护凭据，保存到独立于应用卸载目录的位置。包内同卷快照不能替代外部备份。

数据库迁移后的回滚需要匹配旧镜像、旧包和数据库快照，仅恢复配置指针不足以回退。首次恢复应在隔离数据、网络和凭据的环境验证，避免消费生产队列或回写生产仓库状态。卸载前先导出备份；向导的保留选项是否符合平台实际清理语义，须以部署报告为准。不要执行 `down -v`、全局 `docker system prune` 或删除无法确认归属的缓存。

停机备份与离线验证示例（在 NAS 仓库目录执行，输出路径必须在应用目录之外）：

```bash
sudo appcenter-cli stop woodpecker
sudo python3 scripts/backup-data --output /external/woodpecker-backup.tar.gz
sudo python3 scripts/restore-data --archive /external/woodpecker-backup.tar.gz --destination /external/new-offline-restore
sudo python3 scripts/prepare-maintenance --operation upgrade --backup /external/woodpecker-backup.tar.gz
```

三个备份脚本必须放在同一目录。归档包含 OAuth、Agent 身份和共享密钥，权限为 0600，不加密；需自行安排受限的独立存储。维护脚本核验当前实例、配置代次、全部文件摘要及无运行任务，写入有效期 15 分钟的一次性许可；它不执行升级，也不能保证平台一定尊重生命周期错误。恢复仅写入全新的离线目录，不启动容器、不恢复任意宿主属主。安装时初始化容器为本应用数据树恢复 Server UID 1000 与 Agent root 归属，并拒绝软链接。

## 诊断与验收

安装/配置失败先查看应用中心错误和脱敏生命周期日志；确认 URL 输入、目录权限、安装卷余量、回调本机端口冲突，以及 Compose `format: raw` 是否受支持。不要导出包含真实 Secret 的 `docker compose config` 展开结果、完整 `docker inspect`、环境文件或带 OAuth code/hook token 的 URL。

HTTP 页面探测只能说明代理和 Server 的部分健康，不能证明 Agent 已通过 gRPC 认证并能创建 Docker 任务。最终需要外网 FN Connect 登录、OAuth 回跳、合法与非法 hook token、成功和故意失败的 push、对应 commit 状态、日志流、并发排队和真实资源配额，再覆盖配置重建、持久化与维护场景。每项记录脱敏证据，未通过项保留明确限制。

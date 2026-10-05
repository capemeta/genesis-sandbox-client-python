# Python SDK 发布指南

本指南面向 `capemeta/genesis-sandbox-client-python`，首次公开版本为 `0.1.0`。SDK 版本独立于服务端和其他语言客户端。公开上传由维护者推送版本标签触发，不需要长期 PyPI Token。

## 1. 账号与发布目标

- [PyPI 注册](https://pypi.org/account/register/)：已有账号可直接登录，验证邮箱、启用双因素认证并保存恢复码。
- [GitHub 仓库](https://github.com/capemeta/genesis-sandbox-client-python)：需要管理环境及推送发布标签的权限。
- [PyPI 账号设置](https://pypi.org/manage/account/)：检查邮箱、双因素认证和恢复码。

PyPI 分发名为 `genesis-sandbox-client-python`，Python 导入名为 `genesis_sandbox_client`。发布包和 README 会公开可见。

## 2. 只需配置一次的账号绑定

1. GitHub 仓库打开 `Settings → Environments → New environment`，创建名称为 `pypi` 的环境。
2. 登录 PyPI，打开 [Publishing](https://pypi.org/manage/account/publishing/)，在 Pending Publisher 表单选择 GitHub。
3. 按下表填写并保存。

| 字段 | 值 |
| --- | --- |
| PyPI Project Name | `genesis-sandbox-client-python` |
| Owner | `capemeta` |
| Repository name | `genesis-sandbox-client-python` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

工作流字段只填写文件名，不填写 `.github/workflows/` 路径。首次上传会创建项目并将 Pending Publisher 转为普通 Trusted Publisher；保存表单不会预留包名。若该包已经在你的 PyPI 账号下创建，应改从项目 `Manage → Publishing` 添加同样的绑定。

## 3. 本地检查

在 PowerShell 执行，所有 Python 检查使用 SDK 自己的虚拟环境，不修改 Platform 环境。

```powershell
Set-Location D:\Work\workspace\go\sandbox\genesis-sandbox-client-python
uv sync --locked --python 3.12 --dev
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m mypy src/genesis_sandbox_client
.\.venv\Scripts\python.exe -m pytest -q
uv build --out-dir dist/0.1.0
uv tool run --from twine==6.2.0 twine check dist/0.1.0/*
uv pip install --python .venv/Scripts/python.exe --target .release-check/0.1.0 --no-deps dist/0.1.0/genesis_sandbox_client_python-0.1.0-py3-none-any.whl
.\.venv\Scripts\python.exe -I scripts/verify_release.py .release-check/0.1.0 dist/0.1.0/genesis_sandbox_client_python-0.1.0-py3-none-any.whl
```

独立安装验证会核对包元数据、公开版本号、许可证、类型标记和关键协议调用，拒绝通过源码导入冒充 wheel 安装。产物按版本存放；旧 `0.3.0` wheel 和源码包已删除，本次仅上传 `0.1.0` 分发包。对于已经存在的验证目录，请为新构建选择新的目录名，以确保验证干净安装。

默认测试不连接真实服务。真实服务验收需另外设置 `GENESIS_SANDBOX_INTEGRATION=1`、服务地址和个人测试凭据；未设置时相关测试跳过，不代表已经验证 Linux/Docker 隔离。

## 4. 提交发布源码

```powershell
git status --short
git diff --check
git diff
```

检查并提交本次发布需要的全部源码、测试、`scripts/` 下的发布与验证脚本、根目录 `publish.bat`、README、CHANGELOG、`pyproject.toml`、`uv.lock`、发布指南及 `.github/workflows/publish.yml`。尤其要确认已有新增文件已经纳入 Git，不能只提交版本文件。不要提交 `dist/`、虚拟环境或真实凭据。

推送代码后，在 GitHub Actions 确认 `Python SDK checks and publish` 的 build 任务通过。普通 main 分支推送或 PR 只检查，不上传 PyPI；版本标签才触发上传。

## 5. 执行首次正式发布

推荐使用仓库根目录的 `publish.bat`。BAT 与配套 PowerShell 脚本全部使用英文提示，不需要保存 PyPI Token；实际上传继续由 GitHub Trusted Publisher 执行。脚本自动读取 `pyproject.toml` 的版本并检查 `__version__`，例如 `0.1.0` 自动对应标签 `v0.1.0`。

```powershell
# 仅校验、构建和独立安装；允许尚未提交的草稿，不打标签、不推送、不上传。
.\publish.bat -DryRun

# 提交并推送已审核的发布改动、完成第 2 节账号绑定后执行正式发布。
.\publish.bat
```

正式模式拒绝未提交改动、版本不一致、已存在的标签、错误的 origin 仓库及失败的检查。检查通过后推送当前分支 HEAD，创建并推送 `v<包版本>` 标签。它不会自动提交改动，也不会更新 Platform。推送成功只表示发布已触发，仍需查看 GitHub Actions 和 PyPI 结果。

每次校验都在 `.release-check/` 下创建新的构建和安装目录，避免旧产物污染。需要 Git、uv 和 Windows PowerShell；SDK Python 环境由 uv 创建，调用固定 `.venv/Scripts/python.exe`。

以下手工标签命令与 BAT 正式模式二选一，不要重复执行：

确认当前 HEAD 是已提交、已验证并已推送的发布提交，`pyproject.toml` 与 `__version__` 均为 `0.1.0`，再执行：

```powershell
git tag -a v0.1.0 -m "Release Python SDK 0.1.0"
git push origin v0.1.0
```

进入 GitHub `Actions` 查看 build 与 publish 任务。如果环境配置了人工审核，在 GitHub 中批准对应发布部署。完成后查看 [PyPI 项目](https://pypi.org/project/genesis-sandbox-client-python/0.1.0/)，确认 wheel、源码包、版本及 README。不要把不同内容反复发布为同一版本。

## 6. 验证 PyPI 安装

使用不存在的新目录创建验证环境，明确使用官方包源。

```powershell
uv venv .pypi-check --python 3.12
uv pip install --python .pypi-check/Scripts/python.exe --index-url https://pypi.org/simple --no-cache "genesis-sandbox-client-python==0.1.0"
.\.pypi-check\Scripts\python.exe -I -c "import importlib.metadata as m; import genesis_sandbox_client as s; assert m.version('genesis-sandbox-client-python') == s.__version__ == '0.1.0'; print(s.__file__)"
```

## 7. 发布成功后接入 Platform

包尚未上传时，不能从正式包源生成包含 `0.1.0` 的 Platform 锁文件。发布成功后，在 Platform `pyproject.toml` 将依赖改为 `genesis-sandbox-client-python==0.1.0`。本地 editable 安装仅用于 SDK 联调。

当前 Platform 默认使用清华镜像；为避免首次发布的镜像同步延迟，可合并以下配置。不要重复定义已有同名 TOML 表。

```toml
[[tool.uv.index]]
name = "pypi-official"
url = "https://pypi.org/simple"
explicit = true

[tool.uv.sources]
genesis-sandbox-client-python = { index = "pypi-official" }
```

```powershell
Set-Location D:\Work\workspace\python\genesis-ai\genesis-ai-platform
uv lock
uv sync --locked --no-dev
.\.venv\Scripts\python.exe -c "import importlib.metadata as m; import genesis_sandbox_client as s; assert m.version('genesis-sandbox-client-python') == s.__version__ == '0.1.0'"
```

提交 Platform 的依赖声明和锁文件，并执行适配器测试。`uv sync --no-dev` 不安装 pytest；测试环境如需恢复开发依赖，使用 `uv sync --locked --dev`。生产镜像验证应使用干净构建；当前 `docker/backend/Dockerfile` 仍复制不存在的 `genesis-ai-platform/uv.toml`，需先核对并修正该构建配置。

## 8. 后续发版与故障排查

### 内容与版本的关系

同一未发布版本可以反复开发；正式发布后，运行时内容、API、依赖或分发元数据有变化，应使用新的版本号，不能通过删除旧文件重新覆盖同一个 PyPI 文件名。镜像缓存、客户端缓存和锁文件哈希也依赖版本对应的固定产物。

- 修复缺陷或兼容的小调整：例如 `0.1.0 → 0.1.1`。
- 增加能力或在当前 0.x 阶段调整契约：例如 `0.1.x → 0.2.0`，在 CHANGELOG 明确兼容性影响。
- 预发布测试：例如 `0.2.0rc1 → 0.2.0rc2 → 0.2.0`，仍为不同的版本。
- 仅更新 GitHub 仓库文档、不重新上传分发包：无需为此发布新版本。

客户端依赖 `==0.1.0` 会继续使用该固定版本；升级到 `0.1.1` 要更新依赖声明和锁文件，再部署。允许版本范围也不代表会自动升级已安装的环境；必须重新解析、安装并验证。

首次公开发布前的开发修改不需要从 `0.1.0` 开始递增每一个提交。发布后无需强行与其他语言客户端或服务端一起升版本。

### 常见问题

- 以 `pyproject.toml` 中的 `project.version` 为权威版本，更新 `src/genesis_sandbox_client/__init__.py` 的公开版本号和 CHANGELOG，执行 `uv lock`。发布入口会自动读取包版本并核验一致性，不能单独指定另一套发布版本。新版本例如 `0.1.1` 使用标签 `v0.1.1`；验证脚本从项目元数据读取目标版本，无需修改脚本。
- 版本标签不一致：修正包版本与发布标签，禁止把 `0.1.0` 代码打成 `v0.3.0`。
- Trusted Publisher 身份不匹配：检查 owner、repository、`publish.yml`、`pypi` 四项，以及 publish 任务的 `id-token: write` 权限。
- 包名已被占用：不要继续上传，先确定项目所有权或调整分发名。
- 已上传文件冲突：不要覆盖已有版本，修复后发布新版本；中途失败应先核对已上传文件是否为同一产物。
- 官方源可安装而镜像不可安装：等待同步，或按第 7 节将 SDK 绑定到官方源。

官方参考：[PyPI 首次可信发布](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/)、[PyPI 工作流](https://docs.pypi.org/trusted-publishers/using-a-publisher/)、[uv 构建发布](https://docs.astral.sh/uv/guides/package/)。

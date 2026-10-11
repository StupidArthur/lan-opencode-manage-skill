# lan-opencode-manage-skill

在局域网里遥控多台 Windows PC 上的 **OpenCode V2**(`opencode web` 或 `opencode serve`)的 agent skill + Python CLI。

- `SKILL.md` —— 可被 opencode 自动发现的 skill:教 agent 把整个机队当资源池来调度(派活 / 盯梢 / 验收 / 并发纪律)。
- `scripts/occtl.py` —— Python 3.11 + httpx 单文件 CLI,封装 opencode HTTP API:会话管理、prompt、SSE 事件流、断流对账。
- `scripts/test_occtl.py` —— 24 个单元用例,内建假 opencode 服务器(含 SSE),无需真实环境。
- `scripts/test_integration.py` —— 真实服务器集成测试(环境变量开关,默认跳过)。

## 目标机启动(Windows CMD,每次运行时设置密码)

**不需要设置 Windows 用户级/系统级环境变量,不要执行 `setx`。** 在每台工作机的 **CMD** 中运行以下**一行命令**,用真实强密码替换占位符:

```bat
:: Web 界面 + HTTP API（需要浏览器界面时）
set "OPENCODE_SERVER_PASSWORD=replace-with-strong-password" && opencode web --hostname 0.0.0.0 --port 4096
```

如果只需要让主控 Agent 通过 HTTP API 派工,可改用无浏览器界面的 `serve`(二选一,不要同时占用同一端口):

```bat
set "OPENCODE_SERVER_PASSWORD=replace-with-strong-password" && opencode serve --hostname 0.0.0.0 --port 4096
```

- 用户名**默认就是 `opencode`**,不必设置 `OPENCODE_SERVER_USERNAME`。如已有此环境变量的自定义值,请先在当前 CMD 用 `set "OPENCODE_SERVER_USERNAME="` 清除。
- `OPENCODE_SERVER_PASSWORD` 是 OpenCode V2 的**正确认证变量名**;旧文档里的 `OPENCODE_PASSWORD` 不是服务端认证变量。未设置服务端密码不应被视为安全。
- CMD 的 `set` 只影响当前命令提示符及其子进程,**不会写入用户级或系统级持久环境变量**。关闭 CMD 即失效;停止服务后也可运行 `set "OPENCODE_SERVER_PASSWORD="` 清除当前窗口中的值。
- `opencode web` 提供浏览器界面和 API; `opencode serve` 提供无界面 API。两者均可被本工具管理。
- 用 `--hostname 0.0.0.0` 允许局域网连接;端口为 `4096`。仅限可信 LAN,不要将端口映射公网。含 `%\` 等 CMD 特殊字符的密码需要按 CMD 规则正确转义。

首次需要放行 Windows 防火墙(以管理员身份运行 CMD,限制为本地子网):

```bat
netsh advfirewall firewall add rule name="opencode-web" dir=in action=allow protocol=TCP localport=4096 remoteip=localsubnet
```

从主控机验证（会提示输入密码,避免将密码写在命令行里）：

```bat
curl.exe -u opencode http://192.168.1.88:4096/api/info
```

官方参考：[OpenCode 服务器与认证](https://opencode.ai/docs/zh-cn/server)。

## 安装(主控机 Windows CMD)

```bat
py -3.11 -m pip install httpx
```

把本仓库目录放进 opencode 的 skill 发现路径即可:

- 项目级: `.opencode/skills/lan-opencode-manage-skill/`
- 全局: `%USERPROFILE%\\.config\\opencode\\skills\\lan-opencode-manage-skill\\`

## 快速开始(CMD)

```bat
:: 单机直连:可用 --password 传入,或在当前 CMD 中设置 OCCTL_PASSWORD
py -3.11 scripts\\occtl.py --server http://192.168.1.11:4096 --password "your-password" info

:: 多机:复制示例(只需操作一次),填好每台机器的 url / password / model
copy references\\servers.example.json occtl-servers.json

:: 查看机队
py -3.11 scripts\\occtl.py ps

:: 派活(不阻塞,返回 sessionID)
py -3.11 scripts\\occtl.py --server PC88 --json run --detach --require-idle --dir D:/work/project-a "只在 PC88 分支开发并 push,不得合并主分支"

:: 流式跑完(等终态)
py -3.11 scripts\\occtl.py --server PC88 run --dir D:/work/project-a "任务描述"
```

`occtl-servers.json` 是**主控侧**的机器地址及密码清单;与**工作机启动时**临时设置的 `OPENCODE_SERVER_PASSWORD` 应逐台对应。该文件含明文密码,禁止提交或分享。通过机器名访问时,客户端优先使用清单中该机器的密码,不会被主控 CMD 中同名的服务端密码覆盖。

CLI 客户端密码优先级: `--password` > `OCCTL_PASSWORD` > 机器清单的 `password` > `OPENCODE_SERVER_PASSWORD`(仅作为末级兜底;旧 `OPENCODE_PASSWORD` 仍兼容)。默认 Basic Auth 用户名固定为 `opencode`。

命令: `info / servers / ps / ls / new / get / rm / messages / run(--detach --require-idle) / prompt(--wait) / wait / interrupt / events`,全部支持 `--json`。

退出码: `0` 成功 · `1` 用法/HTTP 错误 · `2` 会话执行失败 · `3` 超时(已自动 interrupt,会话可续用)。

## 每台机器绑定模型

`occtl-servers.json` 里每台机器可以带一个 `model` 字段,派活时自动生效——**不必在命令行写 `--model`**:

```json
{
  "servers": {
    "PC88": { "url": "http://192.168.1.88:4096", "password": "…", "model": "opencode-go/deepseek-v4.1-flash" },
    "PC89": { "url": "http://192.168.1.89:4096", "password": "…", "model": "opencode-go/glm-5.3-flash" }
  }
}
```

取值优先级:`--model` > 环境变量 `OCCTL_MODEL` > 该机 `model` > 目标机 OpenCode 默认模型(由目标机自行选择,CLI 不会主动查询)。

> 为什么要有这个:可以为不同 PC 指定不同模型,便于调度与管理额度;实际服务商凭据、订阅及配额仍需在各工作机正确配置。`servers --probe` 和 `ps` 展示的是**配置的默认模型**(`configured_model`),不是正在运行的会话实际模型;命令行/环境变量可以覆盖它。

## 多机 Git 协作规则

- **管理侧一个目录只对应一个项目/一个 Git 仓库**,通过当前目录的 `origin` 识别项目;不维护全局项目注册表。
- **机器名即长期分支名**:例如服务器 `192.168.1.88` 命名为 `PC88`,`192.168.1.89` 命名为 `PC89`。在每个项目仓库,PC88 只在 `PC88` 分支提交与推送,PC89 只在 `PC89` 分支提交与推送;不按任务创建新分支。
- **主控独占合并与派工决定权**:各 PC 自行 clone/fetch、开发测试、`commit + push` 自己的分支,但不能写主分支或其他 PC 的分支、不能自行向主分支合并或领取下一任务。
- **每轮任务必须过闸**:主控审查机器分支的 diff/测试,选择合并、退回修复或放弃;已合并的分支先安全同步主分支,才给该 PC 派下一任务。多 PC 可以并行,单 PC 不会自行连续接任务。
- **分支历史**:长期固定 PC 分支默认使用保留祖先关系的 merge commit/fast-forward 合并,便于之后 `git merge --ff-only origin/<主分支>` 同步;遇到非快进、未提交修改时停止并请主控处理。建议在 Git 服务端保护主分支并配置机器级推送权限,文档约束本身不能防止越权推送。

详细执行步骤见 [SKILL.md](SKILL.md#git-多机协作固定-pc-分支主控审批)。管理项目中产生的 `.ocmanage/` 台账应被**管理项目根目录**的 `.gitignore` 排除;本 Skill 仓库的 `.gitignore` 不会自动作用于其他项目。

## 版本兼容

自动适配 opencode 的 stable(v2.0.x)与 dev/下一版两套 API 形状,无需配置:

| 能力 | stable(v2.0.x) | dev / 下一版 | occtl 行为 |
|---|---|---|---|
| prompt 请求体 | `{"text": ...}` | `{"prompt": {"text": ...}}` | 先发 stable 形状,400 时自动换下一版形状 |
| wait 路径 | `/api/experimental/session/{id}/wait` | `/api/session/{id}/wait` | 先试 experimental,404 时自动回退 |
| 服务信息 | `GET /api/info` | `GET /api/server` | 先试 `/api/info`,404 时自动回退 |
| create 的 `permissions` | 支持并回显 | 不接受该字段 | 被拒时自动去掉重试,并在 stderr 提醒 |

另:事件流被掐断时,`run/prompt --wait` 会**自动向服务器对账**会话 `outcome`,并**以最终 assistant 消息校准正文**(断流前可能只收到部分 delta);`--require-idle` 查询失败时 **fail-closed** 拒绝派发。

**判定基准**(按优先级):运行中服务器的 `/openapi.json` → 官方发布版 spec → 真机 E2E → dev 文档(仅作前瞻兼容参考)。

## 测试(Windows CMD)

```bat
:: 单元测试(假服务器,不需要真实 OpenCode)
py -3.11 -m unittest scripts.test_occtl -v
```

真实服务器集成测试需要**两个 CMD 窗口**（默认会跳过未设置变量的测试）:

```bat
:: CMD 窗口 1:启动测试服务
set "OPENCODE_SERVER_PASSWORD=replace-with-test-password" && opencode serve --hostname 127.0.0.1 --port 18995
```

```bat
:: CMD 窗口 2:连接测试服务
set "OCCTL_TEST_SERVER=http://127.0.0.1:18995" && set "OCCTL_TEST_PASSWORD=replace-with-test-password" && py -3.11 -m unittest scripts.test_integration -v
```

远程机器测试时用 `OCCTL_TEST_DIR=D:/work/project-a` 指定目标机已有目录。

## 安全

opencode web API 包含 shell / pty / 文件读写接口 —— 等价于目标机的**任意命令执行**:

- 仅限**可信内网**使用,绝不要把端口映射到公网;
- 密码用强随机值;服务端仅在启动 CMD 中临时 `set`,不写持久环境变量;不要提交 `occtl-servers.json`(已默认 gitignore);
- 不用时把目标机的 `opencode web` 或 `opencode serve` 停掉。

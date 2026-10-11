# lan-opencode-manage-skill

在局域网里遥控多台机器上的 **opencode**(v2,web 模式)的 agent skill + Python CLI。

- `SKILL.md` —— 可被 opencode 自动发现的 skill:教 agent 把整个机队当资源池来调度(派活 / 盯梢 / 验收 / 并发纪律)。
- `scripts/occtl.py` —— Python 3.11 + httpx 单文件 CLI,封装 opencode HTTP API:会话管理、prompt、SSE 事件流、断流对账。
- `scripts/test_occtl.py` —— 20 个单元用例,内建假 opencode 服务器(含 SSE),无需真实环境。
- `scripts/test_integration.py` —— 真实服务器集成测试(环境变量开关,默认跳过)。

## 目标机准备(每台 PC,一次)

Windows:

```bat
set OPENCODE_PASSWORD=<强密码>
opencode serve --hostname 0.0.0.0 --port 4096

:: 防火墙放行(管理员)
netsh advfirewall firewall add rule name="opencode-web" dir=in action=allow protocol=TCP localport=4096
```

macOS / Linux:

```sh
OPENCODE_PASSWORD=<强密码> opencode serve --hostname 0.0.0.0 --port 4096
```

验证:`curl -u opencode:<密码> http://<PC-IP>:4096/api/info` 返回 `{"version":...}` 即通。

> `OPENCODE_PASSWORD` 不设则每次启动随机生成;后台 service 默认只绑 127.0.0.1,不适用。

## 安装

```sh
python3.11 -m pip install httpx
```

把本仓库目录放进 opencode 的 skill 发现路径即可:

- 项目级:`.opencode/skills/lan-opencode-manage-skill/`
- 全局:`~/.config/opencode/skills/lan-opencode-manage-skill/`

## 快速开始

```sh
# 单机
python3.11 scripts/occtl.py --server http://192.168.1.11:4096 --password "$PW" ps

# 多机:复制 references/servers.example.json 为 occtl-servers.json 并填 url/密码
cp references/servers.example.json occtl-servers.json
python3.11 scripts/occtl.py ps

# 派活(不阻塞,拿 sessionID;--require-idle 让程序拒绝在有任务执行的机器上再派)
python3.11 scripts/occtl.py --server PC88 --json run --detach --require-idle \
    --dir D:/work/project-a "只在 PC88 分支开发并 push,不得合并主分支;任务…"

# 流式跑完(等终态)
python3.11 scripts/occtl.py --server PC88 run --dir D:/work/project-a "任务…"
```

命令:`info / servers / ps / ls / new / get / rm / messages / run(--detach --require-idle) / prompt(--wait) / wait / interrupt / events`,全部支持 `--json`。

退出码:`0` 成功 · `1` 用法/HTTP 错误 · `2` 会话执行失败 · `3` 超时(已自动 interrupt,会话可续用)。

## 多机 Git 协作规则

- **管理侧一个目录只对应一个项目/一个 Git 仓库**,通过当前目录的 `origin` 识别项目;不维护全局项目注册表。
- **机器名即长期分支名**:例如服务器 `192.168.1.88` 命名为 `PC88`,`192.168.1.89` 命名为 `PC89`。在每个项目仓库,PC88 只在 `PC88` 分支提交与推送,PC89 只在 `PC89` 分支提交与推送;不按任务创建新分支。
- **主控独占合并与派工决定权**:各 PC 自行 clone/fetch、开发测试、`commit + push` 自己的分支,但不能写主分支或其他 PC 的分支、不能自行合并或领取下一任务。
- **每轮任务必须过闸**:主控审查机器分支的 diff/测试,选择合并、退回修复或放弃;已合并的分支先安全同步主分支,才给该 PC 派下一任务。多 PC 可以并行,单 PC 不会自行连续接任务。
- **分支历史**:长期固定 PC 分支默认使用保留祖先关系的 merge commit/fast-forward 合并,便于之后 `git merge --ff-only origin/<主分支>` 同步;遇到非快进、未提交修改时停止并请主控处理。建议在 Git 服务端保护主分支并配置机器级推送权限,文档约束本身不能防止越权推送。

详细执行步骤见 [SKILL.md](SKILL.md#git-多机协作固定-pc-分支主控审批)。

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

## 测试

```sh
# 单元测试(假服务器,默认):20 个用例
python3.11 -m unittest scripts.test_occtl -v

# 真实服务器集成测试(默认跳过):
OPENCODE_PASSWORD=$PW opencode serve --hostname 127.0.0.1 --port 18995 &
OCCTL_TEST_SERVER=http://127.0.0.1:18995 OCCTL_TEST_PASSWORD=$PW \
    python3.11 -m unittest scripts.test_integration -v
# 远程机器时加 OCCTL_TEST_DIR=D:/work/project-a 指定目标机上的目录
```

## 安全

opencode web API 包含 shell / pty / 文件读写接口 —— 等价于目标机的**任意命令执行**:

- 仅限**可信内网**使用,绝不要把端口映射到公网;
- 密码用强随机值,优先走环境变量;不要提交 `occtl-servers.json`(已默认 gitignore);
- 不用时把目标机的 `opencode serve` 停掉。

---
name: OpenCode LAN
description: 通过 HTTP API 遥控局域网内已开启 web 模式(opencode serve)的 opencode:探活、列/建/删会话、发任务并流式回显、多轮续用、打断。当需要驱动另一台 PC 上的 opencode,或查看/继续它的会话时使用。
---

# OpenCode LAN

用 `scripts/occtl.py`(Python 3.11 + httpx)控制局域网里以 `opencode serve` 运行的 opencode。
适用场景:Manager/助手远程驱动各台 PC 上的 opencode 干活;查看并续用某台机器上已有的会话。

## 前置:目标 PC 已开启 web 模式

```bat
:: 固定密码(不设则每次启动随机生成)+ 绑定到局域网
set OPENCODE_PASSWORD=<强密码>
opencode serve --hostname 0.0.0.0 --port 4096

:: 防火墙放行一次(管理员)
netsh advfirewall firewall add rule name="opencode-web" dir=in action=allow protocol=TCP localport=4096
```

验证:`curl -u opencode:<密码> http://<PC-IP>:4096/api/info` 返回 `{"version":...}` 即通。

## 用法

```sh
python3.11 -m pip install httpx        # 首次

# 直连 URL(密码也可走 OCCTL_PASSWORD / OPENCODE_PASSWORD 环境变量)
python3.11 scripts/occtl.py --server http://192.168.1.11:4096 --password "$PW" info

# 多台机器:复制 references/servers.example.json 为 occtl-servers.json(或 ~/.config/occtl/servers.json)
python3.11 scripts/occtl.py --server pc-01 info
```

常用命令(全部支持 `--json`):

| 命令 | 说明 |
|------|------|
| `info` | 探活/服务信息 |
| `servers [--probe]` | 列出 servers.json 里的机器 |
| `ps` | **全队视图**:每台在线状态 + 正在跑的会话(管理入口) |
| `ls [--dir D:/w] [--limit N]` | 会话列表(含 outcome) |
| `run --dir D:/w "任务"` | **新建会话**跑任务,流式输出,等结束 |
| `run --dir D:/w --detach "任务"` | **派完即返**,只拿 sessionID(调度用) |
| `run --session ses_x "追加要求"` | **续用会话**再跑一轮 |
| `prompt ses_x "..." [--wait]` | 只发消息(不新建会话) |
| `messages ses_x [--limit N]` | 读会话消息(最新 assistant 文本 + idle.outcome) |
| `get / rm ses_x` | 会话详情 / 删除 |
| `interrupt ses_x` | 打断当前执行 |
| `events [--session ses_x] [--timeout S]` | 订阅事件流 |

### 跑任务的典型流程

```sh
# 1) 新建并跑(输出:会话 id 在 stderr,正文流式打到 stdout)
python3.11 scripts/occtl.py --server pc-01 --json run --dir D:/work/project-a "把 render/config.yaml 调到 4K/30fps"
# => {"sessionID":"ses_xxx","status":"succeeded","text":"..."}

# 2) 用上一步的 sessionID 续一轮(上下文保留)
python3.11 scripts/occtl.py --server pc-01 --json run --session ses_xxx "把改动提交到本人分支"

# 3) 会话不删会一直留在该 PC 的 opencode 里,可随时 ls 找到、get 查看、rm 删除
```

- `--dir` 必须是**目标 PC 上的路径**(如 `D:/work/project-a`),不要用本机路径。
- 非 `--json` 时:正文流式打印到 stdout,状态行打到 stderr;`--json` 时只输出最终 JSON(含 `text`)。
- 退出码:`0` 成功 · `1` 用法/HTTP 错误 · `2` 会话执行失败 · `3` 超时(已自动 interrupt,会话可续用)。
- 新建会话默认注入"全部允许"权限,headless 不会卡权限询问;需要收紧时用 `--no-allow-all` 并自行在目标机配置 `permissions`。

## 管理规程(AI 当调度器)

这个技能把"管理"放在 agent 层:skill 是作业手册,occtl 是手,agent 自己当调度器。
遵循下面的约定,单台机器就能管住整个机队。

### 数据结构

- `servers.json`(本仓库根目录或 `~/.config/occtl/servers.json`):机器名 → url/password。**唯一机器清单**。
- 任务台账(建议在管理机维护,例如 `fleet-ledger.jsonl`),每行一条:

  ```json
  {"jobId":"20261009-01","pc":"pc-01","sessionID":"ses_x","dir":"D:/work/project-a","prompt":"...","state":"dispatched","createdAt":1791549000000,"endedAt":null,"summary":null}
  ```

  state 取值:`dispatched`(已派)→ `running`(ps/messages 看到在跑)→ `done`/`failed`(终态)→ `accepted`/`rejected`(验收)。
  终态依据:会话 `outcome`(succeeded/failed/interrupted)+ 最新 assistant 文本摘要。

### 派活(不阻塞)

```sh
python3.11 scripts/occtl.py ps                                   # 1) 先看哪台空闲
python3.11 scripts/occtl.py --server pc-01 --json run --detach \
    --dir D:/work/project-a "任务文本"                            # 2) 派发,拿 sessionID
# 3) 立刻把 {jobId, pc, sessionID, dir, prompt} 写进台账
```

### 盯梢

```sh
python3.11 scripts/occtl.py ps                                   # 全队:谁在跑
python3.11 scripts/occtl.py --server pc-01 --json messages ses_x --limit 5
# → 看 newest assistant 文本与 idle.outcome 判断 done/failed
python3.11 scripts/occtl.py --server pc-01 run --session ses_x "..."  # 需要时续轮
```

### 验收与收尾(逐任务)

1. 终态为 succeeded 后,在**同一目录**开验收会话:`run --dir <同目录> "git status 和 git diff,把变更摘要和风险点列出来"`(目标机有自己的 git 身份,不要替它管理账号);
2. 人工/agent 判断通过 → 让它 `commit + push` 到本人分支;不通过 → 台账记 `rejected` 并决定重试或放弃;
3. 会话**不删**,便于追问;确认无用后 `rm`,并把台账行更新到底。

### 并发纪律(重要)

- **每台 PC 同时最多 1 个干活任务**:web 模式没有队列,靠这条纪律约束;派活前先 `ps` 看目标机 `running` 数;
- 不同 PC 可并行;同一 PC 的第二个任务等前一个终态后再派;
- 派发成功的第一步就是写台账——sessionID 丢了就等于任务丢了。

## 安全

opencode web API 包含 shell / pty / 文件读写接口,**等价于该机器的任意命令执行**:

- 仅在**可信内网**使用,绝不要把 4096 端口映射到公网;
- 密码必须强,优先用环境变量传递,不要写进脚本或聊天记录;
- 不需要时把目标机的 `opencode serve` 停掉。

## 排障

| 现象 | 处理 |
|------|------|
| `HTTP 401 UnauthorizedError` | 密码不对;确认目标机 `OPENCODE_PASSWORD` 与传入一致 |
| `连不上服务器` | 目标机没起 serve / 防火墙未放行 / IP 不对 |
| `HTTP 404 SessionNotFoundError` | sessionID 不属于该服务器(被删或串机) |
| `事件流 Ns 无数据` | 网络抖动或目标机卡死;已自动 interrupt,可 `run --session` 续 |
| `缺少依赖 httpx` | `python3.11 -m pip install httpx` |

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
python3.11 scripts/occtl.py --server pc-01 --json run --dir D:/work/project-a "核对 origin,使用任务分支 agent/20261009-01,把 render/config.yaml 调到 4K/30fps"
# => {"sessionID":"ses_xxx","status":"succeeded","text":"..."}

# 2) 用上一步的 sessionID 续一轮(上下文保留)
python3.11 scripts/occtl.py --server pc-01 --json run --session ses_xxx "运行测试,提交并推送任务分支;返回分支、Commit SHA 和测试结果"

# 3) 会话不删会一直留在该 PC 的 opencode 里,可随时 ls 找到、get 查看、rm 删除
```

- `--dir` 必须是**目标 PC 上的路径**(如 `D:/work/project-a`),不要用本机路径。
- 非 `--json` 时:正文流式打印到 stdout,状态行打到 stderr;`--json` 时只输出最终 JSON(含 `text`)。
- 退出码:`0` 成功 · `1` 用法/HTTP 错误 · `2` 会话执行失败 · `3` 超时(已自动 interrupt,会话可续用)。
- 新建会话默认注入"全部允许"权限,headless 不会卡权限询问;需要收紧时用 `--no-allow-all` 并自行在目标机配置 `permissions`。
- **版本兼容**:自动适配 opencode v2.0.x(stable)与 dev/下一版两套 API 形状——prompt 请求体(`text` ↔ `prompt.text`)、wait 路径(experimental ↔ 正式)、服务信息(`/api/info` ↔ `/api/server`)、create 权限字段(新版不支持时自动去掉并在 stderr 提醒)。无需配置。
- **断流对账**:事件流被掐断时,自动查会话 `outcome` 并补回最终文本,不会把已完成的任务误报成丢失(见"盯梢")。

## 管理规程(AI 当调度器)

这个技能把"管理"放在 agent 层:skill 是作业手册,occtl 是手,agent 自己当调度器。
遵循下面的约定,单台机器就能管住整个机队。

### 数据结构

- `servers.json`(本仓库根目录或 `~/.config/occtl/servers.json`):机器名 → url/password。**唯一机器清单**。
- 当前项目任务台账(建议放在管理侧该项目的 `.ocmanage/tasks.jsonl`,加入 `.gitignore` 避免提交),每行一条:

  ```json
  {"jobId":"20261009-01","pc":"pc-01","sessionID":"ses_x","dir":"D:/work/project-a","baseSha":"<sha>","branch":"agent/20261009-01","commit":null,"prompt":"...","state":"dispatched","createdAt":1791549000000,"endedAt":null,"summary":null}
  ```

  state 取值:`dispatched`(已派)→ `running`(ps/messages 看到在跑)→ `done`/`failed`(终态)→ `accepted`/`rejected`(验收)。
  终态依据:会话 `outcome`(succeeded/failed/interrupted)+ 最新 assistant 文本摘要。

### Git 多机协作(一个目录一个项目)

- **管理侧一个工作目录只对应一个项目、一个 Git 仓库**。先在项目目录用 `git rev-parse --show-toplevel` 和 `git remote get-url origin` 确认仓库;不在单目录混合管理多个项目,不维护全局项目注册表。
- 全局 `servers.json` 只记录机器。项目和 PC 可动态组合;每次派活明确 PC、目标机目录、仓库 origin、基准分支/Commit SHA、任务分支。
- 远端 Agent 自行 `git clone`(首次)或 `git fetch`(已有);若尚未 clone,先在目标 PC **已存在的父目录**启动初始化会话,clone 后再将 `--dir` 指向仓库。已有目录必须核对 origin,不匹配即停止。
- 派活前先检查远端 `git status`,不得覆盖未提交或未跟踪改动,禁止未经确认的 `reset --hard` / `clean -fd`。
- 每个任务基于指定基准创建独立分支,如 `agent/<jobId>`;不同 PC 可以并行,不可共同直接修改主分支或共用任务分支。
- 远端完成开发与测试后,自行 `commit + push` 到**任务分支**,返回任务 ID、PC、Session ID、分支名、Commit SHA、测试摘要和风险;不得直接合并主分支。
- Manager 从当前项目仓库 fetch 任务分支或检查 PR,以基准提交核对 diff 并验收;通过后由 Manager 协调合并,否则标记 `rejected` 并安排修复。
- **分工**:Git 负责代码共享,Session 负责任务上下文,Manager 负责调度/验收;本地运行态台账不纳入 Git。大型二进制文件酌情用 Git LFS/独立存储。

### 派活(不阻塞)

```sh
python3.11 scripts/occtl.py ps                                   # 1) 先看哪台空闲
python3.11 scripts/occtl.py --server pc-01 --json run --detach \
    --require-idle --dir D:/work/project-a "任务文本"             # 2) 派发,拿 sessionID
# 3) 立刻把 {jobId, pc, sessionID, dir, baseSha, branch, prompt} 写入当前项目台账
```

`--require-idle` 是程序级防呆:派发前先查目标机 `/api/session/active`,有其它会话在跑就直接拒绝(exit 1);这是尽力检查,不是跨管理机的原子锁。

### 盯梢

```sh
python3.11 scripts/occtl.py ps                                   # 全队:谁在跑
python3.11 scripts/occtl.py --server pc-01 --json messages ses_x --limit 5
# → 看 newest assistant 文本与 idle.outcome 判断 done/failed
python3.11 scripts/occtl.py --server pc-01 run --session ses_x "..."  # 需要时续轮
```

`run/prompt --wait` 的事件流被掐断时,occtl 会**自动向服务器对账**:查会话 `outcome`(succeeded/failed/interrupted)并补回最终文本;若仍在跑会明确提示用 `wait`/`messages` 跟踪。不需要重跑任务。

### 验收与收尾(逐任务)

1. 确认任务终态,让远端 Agent 在任务分支运行测试、`commit + push`,返回分支、Commit SHA、测试结果和风险;每台 PC 使用自己的 Git 身份。
2. Manager 在当前项目目录 `git fetch` 任务分支或查看 PR,依据基准 SHA 审查 diff/测试;验收通过后由 Manager 合并,不通过则台账记 `rejected`,让远端继续修复;未经验收不得合并主分支。
3. 将分支、Commit SHA 和验收结果更新到当前项目台账;会话不删便于追问,确认无用后再 `rm`。

### 并发纪律(重要)

- **每台 PC 同时最多 1 个干活任务**:web 模式没有队列,靠这条纪律约束;派活先 `ps` 看目标机 `running` 数,并给 `run --detach` 加 `--require-idle`(程序会拒绝在有任务执行时派发);
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
| 目标机忙时被 `--require-idle` 拒绝 | 正常防呆;等 `ps` 里目标机 `running:0` 再派,或去掉开关自行承担并发 |

## 自检

```sh
# 单元测试(假 opencode + SSE,不需要真实环境;20 个用例)
python3.11 -m unittest scripts.test_occtl -v

# 真实服务器集成测试(起一个 opencode serve 后;默认跳过)
OCCTL_TEST_SERVER=http://127.0.0.1:18995 OCCTL_TEST_PASSWORD=$PW \
    python3.11 -m unittest scripts.test_integration -v
```

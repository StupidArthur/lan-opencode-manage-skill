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
python3.11 scripts/occtl.py --server PC88 info
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
python3.11 scripts/occtl.py --server PC88 --json run --dir D:/work/project-a "确认仓库 origin 与基准 SHA,只在本机固定分支 PC88 上修改 render/config.yaml 到 4K/30fps"
# => {"sessionID":"ses_xxx","status":"succeeded","text":"..."}

# 2) 用上一步的 sessionID 续一轮(上下文保留)
python3.11 scripts/occtl.py --server PC88 --json run --session ses_xxx "运行测试,只提交并推送 PC88 分支;返回 Commit SHA 与测试结果;等待主控验收"

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

- `servers.json`(本仓库根目录或 `~/.config/occtl/servers.json`):机器名 → url/password。**唯一机器清单**,机器名采用 `PC` + IPv4 末段(如 `PC88` → `192.168.1.88`);大小写固定,即该机器在每个项目仓库的分支名。
- 当前项目任务台账(建议放在管理侧该项目的 `.ocmanage/tasks.jsonl`,加入 `.gitignore` 避免提交),每行一条:

  ```json
  {"jobId":"20261009-01","pc":"PC88","sessionID":"ses_x","dir":"D:/work/project-a","baseSha":"<sha>","branch":"PC88","commit":null,"prompt":"...","state":"dispatched","createdAt":1791549000000,"endedAt":null,"summary":null}
  ```

  state 取值:`dispatched`(已派)→ `running`(执行中)→ `done`/`failed`(执行终态)→ `accepted`/`rejected`(主控验收)→ `merged`(已合并);被拒任务在同一机器分支继续修正,不直接启动下一任务。
  终态依据:会话 `outcome`(succeeded/failed/interrupted)+ 最新 assistant 文本摘要。

### Git 多机协作(固定 PC 分支,主控审批)

1. **项目边界**:管理侧一个工作目录只对应一个项目、一个 Git 仓库;用 `git rev-parse --show-toplevel`、`git remote get-url origin` 确认,不建全局项目注册表。远端同一项目的目录可不同,但 `origin` 必须一致。
2. **机器命名**:全局 `servers.json` 中的名称是机器身份,约定 `192.168.1.88 → PC88`、`192.168.1.89 → PC89`。机器名**区分大小写**且在注册表中唯一;地址变化须先核对并更新映射,不可按未知 IP 猜测机器身份。
3. **固定分支**:在**每个项目仓库**里,非主控 `PC88` 只允许在 `PC88` 分支开发、`commit` 和 `push origin PC88`; `PC89` 同理。机器分支长期复用,**不按任务另建 `agent/<jobId>` 分支**。非主控不得向主分支或他机分支提交/推送,不得执行合并或强推;主控独占主分支合并决定权。
4. **远端初始化**:首次由该 PC 的 Agent 在已存在的父目录内 `git clone`,之后改用仓库目录作为 `--dir`;已有仓库先 `git fetch origin`、核对 remote 和 `git status`。不覆盖未提交修改,未经确认不得 `reset --hard`、`clean -fd`。
5. **派活准备**:主控明确 PC、项目、远端目录、基准分支(如 `main`)、基准 Commit SHA 和任务 ID。首次从 `origin/main` 建立同名机器分支;后续仅在**上轮已验收并合并、工作区干净**时同步主分支到该分支。若无法快进或仍有未合并工作,停下并报告主控。
6. **交付**:非主控只在自己的机器分支修改并测试,完成后 `commit + push` 该分支,向主控返回任务 ID、Session ID、分支名、Commit SHA、测试结果和风险;不得自己合并或自行领取下一任务。
7. **主控闸门**:主控 `git fetch` 相应机器分支或查看 PR,核对相对基准的 diff 与测试;决定 `merge`、退回继续修改或放弃。**主控明确处理本轮结果之前,不可向该 PC 派下一任务**;不同 PC 的独立任务可并行。
8. **合并后同步**:默认由主控以**保留原提交祖先关系**的 merge commit / fast-forward 合并到主分支(不默认 squash/rebase 长期机器分支)。主控确认合并后,PC 在干净的本机分支上 `git fetch origin` → `git merge --ff-only origin/main` → `git push origin <PC名>`,成功后才接新任务;快进失败需主控介入,不能擅自重置或强推。
9. **分工与权限**:Git 共享代码,Session 执行任务,主控管派发与验收;本地运行态台账不入 Git。Skill 是行为约束,如需**技术上禁止越权 push**应在 Git 服务器保护主分支并为不同机器配置独立凭据/推送权限。

### 派活(不阻塞)

```sh
python3.11 scripts/occtl.py ps                                   # 1) 先看哪台空闲
python3.11 scripts/occtl.py --server PC88 --json run --detach \
    --require-idle --dir D:/work/project-a "仅在 PC88 分支工作;完成后 push PC88,不得合并" # 2) 派发
# 3) 立刻把 {jobId, pc, sessionID, dir, baseSha, branch, prompt} 写入当前项目台账
```

`--require-idle` 是程序级防呆:派发前先查目标机 `/api/session/active`,有其它会话在跑就直接拒绝(exit 1);这是尽力检查,不是跨管理机的原子锁。

### 盯梢

```sh
python3.11 scripts/occtl.py ps                                   # 全队:谁在跑
python3.11 scripts/occtl.py --server PC88 --json messages ses_x --limit 5
# → 看 newest assistant 文本与 idle.outcome 判断 done/failed
python3.11 scripts/occtl.py --server PC88 run --session ses_x "..."  # 需要时续轮
```

`run/prompt --wait` 的事件流被掐断时,occtl 会**自动向服务器对账**:查会话 `outcome`(succeeded/failed/interrupted)并补回最终文本;若仍在跑会明确提示用 `wait`/`messages` 跟踪。不需要重跑任务。

### 验收与收尾(逐任务)

1. 确认任务终态,让远端只在**与机器同名的分支**(如 `PC88`)完成测试、`commit + push`,返回 Commit SHA、测试与风险;远端不得合并或自主领取新任务。
2. 主控在当前项目目录 `git fetch origin PC88`,按基准 SHA 审核 diff/测试:通过则仅由主控合并主分支;不通过则退回同一 PC 分支修复或明确放弃。主控处理完本轮任务后才能决定是否派下一任务。
3. 合并后让对应 PC 安全快进同步主分支,确认成功再派下一任务;写回机器分支、Commit SHA、`accepted/rejected/merged` 结果到项目台账。保留 Session 便于追问,不用后再 `rm`。

### 并发纪律(重要)

- **每台 PC 同时最多 1 个干活任务**:web 模式没有队列,靠这条纪律约束;派活先 `ps` 看目标机 `running` 数,并给 `run --detach` 加 `--require-idle`(程序会拒绝在有任务执行时派发);
- 不同 PC 可并行;同一 PC 的第二个任务不仅要等前一个执行终态,还要等主控完成验收/合并决定以及固定机器分支同步,不可仅凭 `ps` 空闲就派发;
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

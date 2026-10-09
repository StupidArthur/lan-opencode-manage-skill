# lan-opencode-manage-skill

在局域网里遥控多台机器上的 **opencode**(v2,web 模式)的 agent skill + Python CLI。

- `SKILL.md` —— 可被 opencode 自动发现的 skill:教 agent 把整个机队当资源池来调度(派活 / 盯梢 / 验收 / 并发纪律)。
- `scripts/occtl.py` —— Python 3.11 + httpx 单文件 CLI,封装 opencode v2 HTTP API:会话管理、prompt、SSE 事件流。
- `scripts/test_occtl.py` —— 12 个用例,内建假 opencode 服务器(含 SSE),无需真实 opencode。

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

# 派活(不阻塞,拿 sessionID)
python3.11 scripts/occtl.py --server pc-01 --json run --detach --dir D:/work/project-a "任务…"

# 流式跑完(等终态)
python3.11 scripts/occtl.py --server pc-01 run --dir D:/work/project-a "任务…"
```

命令:`info / servers / ps / ls / new / get / rm / messages / run(--detach) / prompt(--wait) / wait / interrupt / events`,全部支持 `--json`。

退出码:`0` 成功 · `1` 用法/HTTP 错误 · `2` 会话执行失败 · `3` 超时(已自动 interrupt,会话可续用)。

## 测试

```sh
python3.11 -m unittest -v scripts/test_occtl.py
```

## 安全

opencode web API 包含 shell / pty / 文件读写接口 —— 等价于目标机的**任意命令执行**:

- 仅限**可信内网**使用,绝不要把端口映射到公网;
- 密码用强随机值,优先走环境变量;不要提交 `occtl-servers.json`(已默认 gitignore);
- 不用时把目标机的 `opencode serve` 停掉。

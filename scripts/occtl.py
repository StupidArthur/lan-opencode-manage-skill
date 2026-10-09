#!/usr/bin/env python3
"""occtl — 控制局域网内已开启 web 模式(`opencode serve`)的 opencode。

依赖:Python 3.11+ 与 httpx(`python3.11 -m pip install httpx`)。

示例:
    # 探活
    occtl.py --server http://192.168.1.11:4096 --password "$PW" info

    # 跑一个任务(新建会话 + 流式输出 + 等结束)
    occtl.py --server pc-01 run --dir D:/work/project-a "把 render/config.yaml 调到 4K/30fps"

    # 续用会话再跑一轮
    occtl.py --server pc-01 run --session ses_xxx "把刚才的改动提交到本人分支"

    # 事件流 / 会话管理
    occtl.py --server pc-01 events --session ses_xxx --timeout 60
    occtl.py --server pc-01 ls --dir D:/work/project-a
    occtl.py --server pc-01 interrupt ses_xxx

退出码:0=成功;1=用法/HTTP 错误;2=会话执行失败;3=超时。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterator

try:
    import httpx
except ImportError:  # pragma: no cover
    sys.exit("缺少依赖 httpx:请先执行 python3.11 -m pip install httpx")

VERSION = "0.1.0"

DEFAULT_USERNAME = "opencode"
DEFAULT_READ_TIMEOUT = 120.0

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_EXEC_FAILED = 2
EXIT_TIMEOUT = 3

TERMINAL_EVENTS = {
    "session.execution.succeeded": "succeeded",
    "session.execution.failed": "failed",
    "session.execution.interrupted": "interrupted",
}


class CtlError(Exception):
    """带退出码的命令行错误。"""

    def __init__(self, message: str, code: int = EXIT_ERROR):
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# 服务器解析(URL 或 servers.json 里的名字)
# ---------------------------------------------------------------------------


def default_servers_path() -> Path | None:
    env = os.environ.get("OCCTL_SERVERS")
    if env:
        return Path(env).expanduser()
    local = Path.cwd() / "occtl-servers.json"
    if local.is_file():
        return local
    home = Path.home() / ".config" / "occtl" / "servers.json"
    if home.is_file():
        return home
    return None


def load_registry(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CtlError(f"servers 文件不存在: {path}")
    except json.JSONDecodeError as e:
        raise CtlError(f"servers 文件不是合法 JSON: {path} ({e})")
    servers = raw.get("servers", raw)
    if not isinstance(servers, dict):
        raise CtlError(f"servers 文件格式不对: {path}")
    return servers


def resolve_server(args: argparse.Namespace) -> tuple[str, str]:
    """返回 (base_url, password)。"""
    ref = (args.server or os.environ.get("OCCTL_SERVER") or "").strip()
    if not ref:
        raise CtlError("缺少 --server(URL 或 servers.json 里的名字),或用 OCCTL_SERVER 环境变量")

    password = args.password or os.environ.get("OCCTL_PASSWORD") or os.environ.get("OPENCODE_PASSWORD") or ""

    if ref.startswith("http://") or ref.startswith("https://"):
        return ref.rstrip("/"), password

    entry = load_registry(Path(args.servers).expanduser() if args.servers else default_servers_path()).get(ref)
    if entry is None:
        raise CtlError(f"servers.json 里没有名为 {ref!r} 的服务器")
    url = str(entry.get("url", "")).rstrip("/")
    if not url:
        raise CtlError(f"服务器 {ref!r} 缺少 url 字段")
    if not password:
        password = str(entry.get("password", ""))
    return url, password


def make_client(base_url: str, password: str, *, read: float = 30.0, connect: float = 10.0) -> httpx.Client:
    auth = (DEFAULT_USERNAME, password) if password else None
    return httpx.Client(
        base_url=base_url,
        auth=auth,
        timeout=httpx.Timeout(connect=connect, read=read, write=30.0, pool=10.0),
        headers={"accept": "application/json"},
    )


def unwrap(resp: httpx.Response) -> Any:
    """检查状态码并解开 {data: ...} 信封。"""
    if resp.status_code >= 300:
        tag, message, body = "", "", ""
        try:
            payload = resp.json()
            tag = str(payload.get("_tag", ""))
            message = str(payload.get("message", ""))
        except Exception:
            body = resp.text.strip()[:300]
        detail = message or body or "(无错误信息)"
        raise CtlError(f"HTTP {resp.status_code} {tag}: {detail}".strip())
    if resp.status_code == 204 or not resp.content:
        return None
    try:
        payload = resp.json()
    except json.JSONDecodeError:
        return resp.text
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def interrupt_quietly(client: httpx.Client, session_id: str) -> None:
    try:
        client.post(f"/api/session/{session_id}/interrupt", params={"resume": "false"})
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 版本兼容层
# ---------------------------------------------------------------------------
# opencode v2.0.x(stable)与 dev/下一版存在 API 形状差异:
#
#   能力            stable                         next(dev)
#   prompt 请求体   {"text": ...}                  {"prompt": {"text": ...}}
#   wait 路径       /api/experimental/session/...  /api/session/{id}/wait
#   服务信息        GET /api/info                  GET /api/server
#   create 权限     permissions 字段被接受并回显    不接受该字段
#
# 这里按"先 stable 后回退"探测,并按 base_url 缓存探测结果;两种版本都能跑。

_SHAPES: dict[str, dict[str, str]] = {}


def _shape(base_url: str) -> dict[str, str]:
    return _SHAPES.setdefault(base_url, {})


def _is_validation_error(exc: CtlError) -> bool:
    msg = str(exc)
    return "HTTP 400" in msg or "HTTP 422" in msg


def server_info(client: httpx.Client, base_url: str) -> Any:
    """GET /api/info;404 时回退 GET /api/server(next 版)。"""
    if _shape(base_url).get("info") == "server":
        return unwrap(client.get("/api/server"))
    try:
        return unwrap(client.get("/api/info"))
    except CtlError as e:
        if "HTTP 404" not in str(e):
            raise
    info = unwrap(client.get("/api/server"))
    _shape(base_url)["info"] = "server"
    return info


def post_prompt(
    client: httpx.Client,
    base_url: str,
    session_id: str,
    text: str,
    *,
    delivery: str | None = None,
    resume: bool | None = None,
) -> Any:
    """POST prompt,自动兼容 flat(text)与 nested(prompt.text)两种请求体。"""
    extra: dict[str, Any] = {}
    if delivery:
        extra["delivery"] = delivery
    if resume is not None:
        extra["resume"] = resume
    if _shape(base_url).get("prompt") == "nested":
        return unwrap(client.post(f"/api/session/{session_id}/prompt", json={"prompt": {"text": text}, **extra}))
    try:
        return unwrap(client.post(f"/api/session/{session_id}/prompt", json={"text": text, **extra}))
    except CtlError as e:
        if not _is_validation_error(e):
            raise
    data = unwrap(client.post(f"/api/session/{session_id}/prompt", json={"prompt": {"text": text}, **extra}))
    _shape(base_url)["prompt"] = "nested"
    return data


def wait_session(client: httpx.Client, base_url: str, session_id: str) -> None:
    """POST wait,自动兼容 experimental 与 stable 两种路径。"""
    if _shape(base_url).get("wait") == "stable":
        unwrap(client.post(f"/api/session/{session_id}/wait"))
        return
    try:
        unwrap(client.post(f"/api/experimental/session/{session_id}/wait"))
        return
    except CtlError as e:
        if "HTTP 404" not in str(e):
            raise
    unwrap(client.post(f"/api/session/{session_id}/wait"))
    _shape(base_url)["wait"] = "stable"


def create_session(
    client: httpx.Client, base_url: str, body: dict[str, Any]
) -> tuple[Any, str]:
    """POST /api/session;返回 (会话, 权限状态)。

    权限状态:
      "none"        —— 未请求权限(--no-allow-all)
      "applied"     —— 请求了且回读确认生效
      "not-applied" —— 该版本不接受该字段(已自动去掉重试)
      "unverified"  —— 请求了但回读失败,无法确认是否生效
    """
    wants_perms = "permissions" in body
    if _shape(base_url).get("create") == "no-perms":
        body = {k: v for k, v in body.items() if k != "permissions"}
        return unwrap(client.post("/api/session", json=body)), ("not-applied" if wants_perms else "none")
    try:
        sess = unwrap(client.post("/api/session", json=body))
    except CtlError as e:
        if not wants_perms or not _is_validation_error(e):
            raise
        stripped = {k: v for k, v in body.items() if k != "permissions"}
        sess = unwrap(client.post("/api/session", json=stripped))
        _shape(base_url)["create"] = "no-perms"
        return sess, "not-applied"
    if not wants_perms:
        return sess, "none"
    # stable 版会把 permissions 回显在会话上;回读确认
    sid = (sess or {}).get("id", "")
    if not sid:
        return sess, "unverified"
    try:
        stored = unwrap(client.get(f"/api/session/{sid}")) or {}
    except Exception:
        return sess, "unverified"
    return sess, ("applied" if stored.get("permissions") else "not-applied")


def _warn_permissions(status: str) -> None:
    if status == "not-applied":
        print(
            "[occtl] 注意:该 opencode 版本未接受会话级 permissions,权限未注入;"
            "请在目标机全局配置 permissions(allow)或接受交互式批准",
            file=sys.stderr,
        )
    elif status == "unverified":
        print(
            "[occtl] 注意:无法回读验证会话权限是否生效;请在目标机确认 permissions,"
            "或改用 --no-allow-all 后自行配置",
            file=sys.stderr,
        )


def ensure_idle(client: httpx.Client, session_id: str | None) -> None:
    """--require-idle:目标机没有其它正在执行的会话时才允许派发。

    fail-closed:查询失败(网络/认证/端点缺失/响应异常)一律拒绝派发,
    绝不在"无法确认是否空闲"的情况下放行。
    注意:检查与派发之间并非原子操作,这是尽力而为的防呆,不是分布式锁。
    """
    try:
        active = unwrap(client.get("/api/session/active"))
    except CtlError as e:
        raise CtlError(f"--require-idle 无法确认目标机空闲({e});拒绝派发") from e
    except httpx.HTTPError as e:
        raise CtlError(f"--require-idle 无法确认目标机空闲(网络错误: {e});拒绝派发") from e
    if active is not None and not isinstance(active, dict):
        raise CtlError("--require-idle 无法确认目标机空闲(/api/session/active 返回异常);拒绝派发")
    others = [sid for sid in (active or {}) if sid != session_id]
    if others:
        raise CtlError(
            f"目标机已有正在执行的会话: {', '.join(others)};"
            f"--require-idle 拒绝派发(等它结束,或去掉该开关)"
        )


def recover_session(client: httpx.Client, session_id: str, result: dict[str, Any]) -> str | None:
    """事件流断开后对账会话真实状态。

    返回 "succeeded"/"failed"/"interrupted"/"running",或 None(无法判定)。
    只要拿得到最终 assistant 消息,就用它**校准**正文——断流前可能只收到部分 delta。
    """
    outcome = None
    try:
        sess = unwrap(client.get(f"/api/session/{session_id}")) or {}
        outcome = sess.get("outcome")
    except Exception:
        pass
    if not outcome:
        try:
            active = unwrap(client.get("/api/session/active")) or {}
            if session_id in active:
                return "running"
        except Exception:
            pass
        return None
    # 以最终 assistant 消息校准文本(可能比已累积的 delta 更完整)
    try:
        msgs = unwrap(
            client.get(f"/api/session/{session_id}/message", params={"limit": 20, "order": "desc"})
        ) or []
        for m in msgs:
            if m.get("type") == "assistant":
                text = "".join(
                    p.get("text", "") for p in (m.get("content") or []) if p.get("type") == "text"
                )
                if text:
                    result["text"] = text
                    break
    except Exception:
        pass
    return outcome


# ---------------------------------------------------------------------------
# SSE 事件流
# ---------------------------------------------------------------------------


def iter_events(resp: httpx.Response) -> Iterator[dict[str, Any]]:
    for line in resp.iter_lines():
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload:
            continue
        try:
            yield json.loads(payload)
        except json.JSONDecodeError:
            continue


def open_event_stream(base_url: str, password: str, read_timeout: float) -> tuple[httpx.Client, httpx.Response]:
    """打开 /api/event 流;调用方负责 closed_stream()。"""
    client = make_client(base_url, password, read=read_timeout)
    req = client.build_request("GET", "/api/event")
    req.headers["accept"] = "text/event-stream"
    resp = client.send(req, stream=True)
    if resp.status_code >= 300:
        resp.close()
        client.close()
        raise CtlError(f"HTTP {resp.status_code}: 无法订阅事件流(检查地址与密码)")
    return client, resp


def closed_stream(client: httpx.Client, resp: httpx.Response) -> None:
    resp.close()
    client.close()


# ---------------------------------------------------------------------------
# 输出辅助
# ---------------------------------------------------------------------------


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def fmt_session(s: dict[str, Any]) -> str:
    loc = (s.get("location") or {}).get("directory", "")
    title = s.get("title", "")
    updated = (s.get("time") or {}).get("updated", 0)
    when = time.strftime("%m-%d %H:%M", time.localtime(updated / 1000)) if updated else "-"
    outcome = s.get("outcome") or ""
    parts = [s.get("id", "?"), when, loc or "-"]
    if title:
        parts.append(title)
    if outcome:
        parts.append(f"[{outcome}]")
    return "  ".join(parts)


# ---------------------------------------------------------------------------
# 命令实现
# ---------------------------------------------------------------------------


def cmd_info(args: argparse.Namespace) -> int:
    base, pw = resolve_server(args)
    with make_client(base, pw) as client:
        info = server_info(client, base) or {}
    if args.json:
        print_json(info)
        return EXIT_OK
    print(f"url      {base}")
    if info.get("version") is not None:
        print(f"version  {info['version']}")
    if info.get("pid") is not None:
        print(f"pid      {info['pid']}")
    urls = info.get("urls") or info.get("url") or []
    if isinstance(urls, str):
        urls = [urls]
    if urls:
        print(f"urls     {', '.join(str(u) for u in urls)}")
    return EXIT_OK


def cmd_servers(args: argparse.Namespace) -> int:
    path = Path(args.servers).expanduser() if args.servers else default_servers_path()
    registry = load_registry(path)
    rows = []
    for name, entry in sorted(registry.items()):
        row: dict[str, Any] = {"name": name, "url": str(entry.get("url", "")).rstrip("/"), "online": None}
        if args.probe:
            try:
                with make_client(row["url"], str(entry.get("password", ""))) as client:
                    server_info(client, row["url"])
                row["online"] = True
            except Exception:
                row["online"] = False
        rows.append(row)
    if args.json:
        print_json({"file": str(path) if path else None, "servers": rows})
    else:
        print(f"# {path or '(未找到 servers.json)'}")
        for row in rows:
            mark = {True: "online", False: "OFFLINE", None: "-"}[row["online"]]
            print(f"{row['name']:<12} {mark:<8} {row['url']}")
    return EXIT_OK


def cmd_ps(args: argparse.Namespace) -> int:
    """全队视图:每台机器的在线状态 + 正在跑的会话(管理原语)。"""
    targets: list[tuple[str, str, str]] = []  # (name, url, password)
    if args.server or os.environ.get("OCCTL_SERVER"):
        base, pw = resolve_server(args)
        targets.append((args.server or base, base, pw))
    else:
        registry = load_registry(Path(args.servers).expanduser() if args.servers else default_servers_path())
        if not registry:
            raise CtlError("没有 servers.json;用 --server 指定单台,或先创建 registry(见 references/servers.example.json)")
        for name, entry in sorted(registry.items()):
            targets.append((name, str(entry.get("url", "")).rstrip("/"), str(entry.get("password", ""))))

    rows = []
    for name, url, pw in targets:
        row: dict[str, Any] = {"name": name, "url": url, "online": False, "active": {}}
        try:
            with make_client(url, pw, connect=3.0, read=5.0) as client:
                row["active"] = unwrap(client.get("/api/session/active")) or {}
            row["online"] = True
        except Exception:
            pass
        rows.append(row)

    if args.json:
        print_json(rows)
        return EXIT_OK
    for row in rows:
        if not row["online"]:
            print(f"{row['name']:<12} OFFLINE   {row['url']}")
            continue
        act = row["active"]
        detail = "  ".join(act.keys()) if act else "-"
        print(f"{row['name']:<12} online   running:{len(act):<3} {detail}")
    return EXIT_OK


def cmd_ls(args: argparse.Namespace) -> int:
    base, pw = resolve_server(args)
    params: dict[str, Any] = {}
    if args.dir:
        params["directory"] = args.dir
    if args.limit:
        params["limit"] = args.limit
    with make_client(base, pw) as client:
        data = unwrap(client.get("/api/session", params=params)) or []
    if args.json:
        print_json(data)
    elif not data:
        print("(没有会话)")
    else:
        for s in data:
            print(fmt_session(s))
    return EXIT_OK


def cmd_new(args: argparse.Namespace) -> int:
    base, pw = resolve_server(args)
    body: dict[str, Any] = {}
    if args.dir:
        body["location"] = {"directory": args.dir}
    if args.title:
        body["title"] = args.title
    if args.agent:
        body["agent"] = args.agent
    if args.model:
        body["model"] = parse_model_ref(args.model)
    if not args.no_allow_all:
        body["permissions"] = [{"action": "*", "resource": "*", "effect": "allow"}]
    with make_client(base, pw) as client:
        sess, perms = create_session(client, base, body)
    _warn_permissions(perms)
    if args.json:
        print_json(sess)
    else:
        print(sess.get("id", ""))
    return EXIT_OK


def parse_model_ref(value: str) -> dict[str, Any]:
    variant = None
    if "#" in value:
        value, variant = value.rsplit("#", 1)
    if "/" in value:
        provider, model_id = value.split("/", 1)
    else:
        provider, model_id = "", value
    ref: dict[str, Any] = {"providerID": provider, "id": model_id}
    if variant:
        ref["variant"] = variant
    return ref


def cmd_get(args: argparse.Namespace) -> int:
    base, pw = resolve_server(args)
    with make_client(base, pw) as client:
        sess = unwrap(client.get(f"/api/session/{args.session_id}"))
    if args.json:
        print_json(sess)
    else:
        print(fmt_session(sess))
    return EXIT_OK


def cmd_rm(args: argparse.Namespace) -> int:
    base, pw = resolve_server(args)
    with make_client(base, pw) as client:
        resp = client.delete(f"/api/session/{args.session_id}")
        if resp.status_code not in (200, 204):
            unwrap(resp)
    print("deleted" if not args.json else json.dumps({"deleted": args.session_id}))
    return EXIT_OK


def cmd_messages(args: argparse.Namespace) -> int:
    base, pw = resolve_server(args)
    params: dict[str, Any] = {}
    if args.limit:
        params["limit"] = args.limit
    if args.order:
        params["order"] = args.order
    with make_client(base, pw) as client:
        data = unwrap(client.get(f"/api/session/{args.session_id}/message", params=params)) or []
    if args.json:
        print_json(data)
        return EXIT_OK
    for m in data:
        kind = m.get("type", "?")
        if kind == "user":
            print(f"[user] {m.get('text', '')}")
        elif kind == "assistant":
            for part in m.get("content") or []:
                if part.get("type") == "text" and part.get("text"):
                    print(f"[assistant] {part['text']}")
        else:
            print(f"[{kind}]")
    return EXIT_OK


def cmd_wait(args: argparse.Namespace) -> int:
    base, pw = resolve_server(args)
    with make_client(base, pw, read=args.timeout or DEFAULT_READ_TIMEOUT) as client:
        wait_session(client, base, args.session_id)
    if args.json:
        print_json({"sessionID": args.session_id, "status": "idle"})
    else:
        print("idle")
    return EXIT_OK


def cmd_interrupt(args: argparse.Namespace) -> int:
    base, pw = resolve_server(args)
    with make_client(base, pw) as client:
        unwrap(client.post(f"/api/session/{args.session_id}/interrupt", params={"resume": "true" if args.resume else "false"}))
    if args.json:
        print_json({"sessionID": args.session_id, "interrupted": True})
    else:
        print("interrupted")
    return EXIT_OK


def cmd_events(args: argparse.Namespace) -> int:
    base, pw = resolve_server(args)
    deadline = time.monotonic() + args.timeout if args.timeout else None
    read_timeout = args.read_timeout
    if deadline is not None:
        read_timeout = max(0.2, min(read_timeout, deadline - time.monotonic()))
    count = 0
    client, resp = open_event_stream(base, pw, read_timeout)
    try:
        for ev in iter_events(resp):
            if deadline is not None and time.monotonic() > deadline:
                break
            if args.session and ev.get("data", {}).get("sessionID") not in (None, args.session):
                continue
            if args.json:
                print(json.dumps(ev, ensure_ascii=False))
            else:
                data = ev.get("data") or {}
                delta = data.get("delta")
                extra = f" {delta}" if delta else ""
                sid = data.get("sessionID", "")
                print(f"{time.strftime('%H:%M:%S')} {ev.get('type', '?')} {sid}{extra}")
            count += 1
            if args.max and count >= args.max:
                break
    finally:
        closed_stream(client, resp)
    return EXIT_OK


def cmd_prompt(args: argparse.Namespace) -> int:
    text = " ".join(args.text)
    base, pw = resolve_server(args)
    if not args.wait:
        with make_client(base, pw) as client:
            if args.require_idle:
                ensure_idle(client, args.session_id)
            item = post_prompt(client, base, args.session_id, text)
        if args.json:
            print_json(item)
        else:
            print(f"queued {(item or {}).get('id', '')}")
        return EXIT_OK
    return stream_turn(base, pw, args.session_id, text, args)


def cmd_run(args: argparse.Namespace) -> int:
    text = " ".join(args.text)
    base, pw = resolve_server(args)
    session_id = args.session

    if not session_id:
        if not args.dir:
            raise CtlError("新建会话必须给 --dir(该 PC 上的项目目录)")
        body: dict[str, Any] = {"location": {"directory": args.dir}}
        if args.title:
            body["title"] = args.title
        if args.agent:
            body["agent"] = args.agent
        if args.model:
            body["model"] = parse_model_ref(args.model)
        if not args.no_allow_all:
            body["permissions"] = [{"action": "*", "resource": "*", "effect": "allow"}]
        with make_client(base, pw) as client:
            if args.require_idle:
                ensure_idle(client, None)
            sess, perms = create_session(client, base, body)
        _warn_permissions(perms)
        session_id = sess.get("id", "")
        if not args.json:
            print(f"[occtl] session {session_id}", file=sys.stderr)

    if args.detach:
        with make_client(base, pw) as client:
            if args.require_idle:
                ensure_idle(client, session_id)
            item = post_prompt(client, base, session_id, text)
        if args.json:
            print_json({"sessionID": session_id, "status": "dispatched", "messageID": (item or {}).get("id", "")})
        else:
            print(f"dispatched session={session_id}(用 ps / messages / prompt --wait 跟踪)")
        return EXIT_OK

    return stream_turn(base, pw, session_id, text, args)


def stream_turn(base: str, pw: str, session_id: str, text: str, args: argparse.Namespace) -> int:
    """发一轮消息,流式打印输出,等终态。返回退出码。"""
    timeout = getattr(args, "timeout", None)
    read_timeout = getattr(args, "read_timeout", DEFAULT_READ_TIMEOUT)
    show_reasoning = getattr(args, "show_reasoning", False)
    deadline = time.monotonic() + timeout if timeout else None

    result: dict[str, Any] = {"sessionID": session_id, "status": "pending", "text": ""}
    finished = threading.Event()  # 结束时叫停看门狗
    timed_out = threading.Event()
    control = make_client(base, pw)

    def report_timeout() -> int:
        result["status"] = "timeout"
        if args.json:
            print_json(result)
        else:
            print()
            print(f"[occtl] 超时({timeout}s),已发送 interrupt;会话 {session_id} 可续用", file=sys.stderr)
        return EXIT_TIMEOUT

    def watchdog():
        if deadline is None:
            return
        if not finished.wait(max(deadline - time.monotonic(), 0)):
            timed_out.set()
            interrupt_quietly(control, session_id)

    client, resp = open_event_stream(base, pw, read_timeout)
    transport_err: Exception | None = None
    sent = False
    try:
        gen = iter_events(resp)
        # 1) 等事件流就绪
        for ev in gen:
            if ev.get("type") == "server.connected":
                break
        # 2) 到点看门狗:即使没有事件也能触发 interrupt,让服务端发事件唤醒主循环
        threading.Thread(target=watchdog, daemon=True).start()
        # 3) 发消息(可选机器级空闲检查)
        if getattr(args, "require_idle", False):
            ensure_idle(control, session_id)
        post_prompt(control, base, session_id, text)
        sent = True
        # 4) 读事件直到终态
        try:
            for ev in gen:
                etype = ev.get("type", "")
                data = ev.get("data") or {}
                sid = data.get("sessionID")
                if sid and sid != session_id:
                    continue
                if timed_out.is_set():
                    return report_timeout()
                if etype == "session.text.delta":
                    delta = data.get("delta", "")
                    result["text"] += delta
                    if not args.json:
                        print(delta, end="", flush=True)
                elif etype == "session.reasoning.delta":
                    if show_reasoning and not args.json:
                        print(data.get("delta", ""), end="", flush=True)
                elif etype in TERMINAL_EVENTS:
                    status = TERMINAL_EVENTS[etype]
                    result["status"] = status
                    if status != "succeeded":
                        err = data.get("error") or {}
                        result["error"] = err.get("message") or err.get("type") or status
                    if not args.json:
                        print()
                        print(f"[occtl] session={session_id} status={status}", file=sys.stderr)
                    if args.json:
                        print_json(result)
                    return EXIT_OK if status == "succeeded" else EXIT_EXEC_FAILED
        except httpx.ReadTimeout:
            interrupt_quietly(control, session_id)
            raise CtlError(f"事件流 {read_timeout}s 无数据,已发送 interrupt", EXIT_TIMEOUT)
        except httpx.HTTPError as e:  # 连接被掐断/协议错误:走对账
            transport_err = e
    except KeyboardInterrupt:
        interrupt_quietly(control, session_id)
        print(f"\n[occtl] 已取消;会话 {session_id} 可续用", file=sys.stderr)
        return 130
    except httpx.ReadTimeout:
        interrupt_quietly(control, session_id)
        raise CtlError(f"事件流 {read_timeout}s 无数据,已发送 interrupt", EXIT_TIMEOUT)
    except httpx.HTTPError as e:
        transport_err = e
    finally:
        finished.set()
        closed_stream(client, resp)
        control.close()

    if timed_out.is_set():
        return report_timeout()
    if not sent:
        raise CtlError(f"事件流断开({transport_err or '未就绪'}),消息未发出", EXIT_ERROR)

    # 事件流断开且没有终态:向服务器对账会话真实状态(避免"实际完成但管理端不知道")
    status = None
    try:
        with make_client(base, pw) as rec:
            status = recover_session(rec, session_id, result)
    except Exception:
        status = None
    if status == "succeeded":
        result["status"] = "succeeded"
        if not args.json:
            print()
            print(f"[occtl] session={session_id} status=succeeded(事件流断开,已向服务器对账)", file=sys.stderr)
        if args.json:
            print_json(result)
        return EXIT_OK
    if status in ("failed", "interrupted"):
        result["status"] = status
        result.setdefault("error", f"session {status}")
        if not args.json:
            print()
            print(f"[occtl] session={session_id} status={status}(事件流断开,经对账确认)", file=sys.stderr)
        if args.json:
            print_json(result)
        return EXIT_EXEC_FAILED
    if status == "running":
        raise CtlError(f"事件流断开但会话仍在执行;用 wait / messages 跟踪 {session_id}", EXIT_ERROR)
    result["status"] = "unknown"
    if args.json:
        print_json(result)
    detail = f":{transport_err}" if transport_err else ""
    raise CtlError(f"事件流意外结束{detail},且无法对账会话状态", EXIT_ERROR)


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="occtl",
        description="控制局域网内已开启 web 模式(opencode serve)的 opencode",
    )
    parser.add_argument("--server", help="http(s)://host:port 或 servers.json 里的名字(env: OCCTL_SERVER)")
    parser.add_argument("--password", help="访问密码(env: OCCTL_PASSWORD / OPENCODE_PASSWORD)")
    parser.add_argument("--servers", help="servers.json 路径(env: OCCTL_SERVERS)")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出(便于脚本/agent 消费)")
    parser.add_argument("--version", action="version", version=f"occtl {VERSION}")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("info", help="服务信息/探活").set_defaults(func=cmd_info)

    p = sub.add_parser("servers", help="列出 servers.json 里的服务器")
    p.add_argument("--probe", action="store_true", help="逐个探活")
    p.set_defaults(func=cmd_servers)

    p = sub.add_parser("ps", help="全队视图:在线状态 + 正在跑的会话")
    p.set_defaults(func=cmd_ps)

    p = sub.add_parser("ls", help="列出会话")
    p.add_argument("--dir", help="按项目目录过滤")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_ls)

    p = sub.add_parser("new", help="新建会话")
    p.add_argument("--dir", required=True, help="项目目录(location)")
    p.add_argument("--title")
    p.add_argument("--agent")
    p.add_argument("--model", help="provider/model[#variant]")
    p.add_argument("--no-allow-all", action="store_true", help="不注入'全部允许'权限")
    p.set_defaults(func=cmd_new)

    p = sub.add_parser("get", help="会话详情")
    p.add_argument("session_id")
    p.set_defaults(func=cmd_get)

    p = sub.add_parser("rm", help="删除会话")
    p.add_argument("session_id")
    p.set_defaults(func=cmd_rm)

    p = sub.add_parser("messages", help="读取会话消息")
    p.add_argument("session_id")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--order", choices=["asc", "desc"])
    p.set_defaults(func=cmd_messages)

    p = sub.add_parser("wait", help="阻塞到会话空闲")
    p.add_argument("session_id")
    p.add_argument("--timeout", type=float, help="等待秒数")
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("interrupt", help="打断当前执行")
    p.add_argument("session_id")
    p.add_argument("--resume", action="store_true", help="打断后保留可续状态")
    p.set_defaults(func=cmd_interrupt)

    p = sub.add_parser("events", help="订阅事件流")
    p.add_argument("--session", help="只看某个会话")
    p.add_argument("--timeout", type=float, help="最多订阅秒数")
    p.add_argument("--max", type=int, help="最多打印 N 条")
    p.add_argument("--read-timeout", type=float, default=DEFAULT_READ_TIMEOUT)
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("prompt", help="发送一轮消息(不新建会话)")
    p.add_argument("session_id")
    p.add_argument("text", nargs="+")
    p.add_argument("--wait", action="store_true", help="流式输出并等待终态")
    p.add_argument("--require-idle", action="store_true", help="目标机有其它会话在跑时拒绝派发(fail-closed;尽力检查,非严格锁)")
    p.add_argument("--timeout", type=float, help="整体超时秒数")
    p.add_argument("--read-timeout", type=float, default=DEFAULT_READ_TIMEOUT)
    p.add_argument("--show-reasoning", action="store_true")
    p.set_defaults(func=cmd_prompt)

    p = sub.add_parser("run", help="跑一个任务(新建或续用会话,流式输出,等终态)")
    p.add_argument("text", nargs="+")
    p.add_argument("--dir", help="新建会话时的项目目录")
    p.add_argument("--session", help="续用已有会话;给了就不再新建")
    p.add_argument("--detach", action="store_true", help="派发后立即返回(不等执行;用 ps/messages/wait 跟踪)")
    p.add_argument("--require-idle", action="store_true", help="目标机有其它会话在跑时拒绝派发(fail-closed;尽力检查,非严格锁)")
    p.add_argument("--title")
    p.add_argument("--agent")
    p.add_argument("--model", help="provider/model[#variant]")
    p.add_argument("--no-allow-all", action="store_true")
    p.add_argument("--timeout", type=float, help="整体超时秒数")
    p.add_argument("--read-timeout", type=float, default=DEFAULT_READ_TIMEOUT)
    p.add_argument("--show-reasoning", action="store_true")
    p.set_defaults(func=cmd_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except CtlError as e:
        print(f"[occtl] {e}", file=sys.stderr)
        return e.code
    except httpx.ConnectError as e:
        print(f"[occtl] 连不上服务器: {e}", file=sys.stderr)
        return EXIT_ERROR
    except httpx.ReadTimeout:
        print("[occtl] 请求超时", file=sys.stderr)
        return EXIT_TIMEOUT
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

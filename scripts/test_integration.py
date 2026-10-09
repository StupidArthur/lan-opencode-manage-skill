#!/usr/bin/env python3
"""真实 opencode 服务器的集成测试(默认跳过,不依赖模拟)。

跑法:

    OPENCODE_PASSWORD=devpass123 opencode serve --hostname 127.0.0.1 --port 18995 &

    OCCTL_TEST_SERVER=http://127.0.0.1:18995 \
    OCCTL_TEST_PASSWORD=devpass123 \
        python3.11 -m unittest test_integration -v

可选变量:

    OCCTL_TEST_DIR   目标机上已存在的项目目录(默认用本机临时目录;
                     若 server 是远程机器,必须显式指定该机上的路径,如 D:/work/project-a)

验证主链路:info → new → prompt --wait(流式+终态)→ messages → ps → rm。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

CLI = Path(__file__).parent / "occtl.py"
SERVER = os.environ.get("OCCTL_TEST_SERVER", "")
PASSWORD = os.environ.get("OCCTL_TEST_PASSWORD", "")
TEST_DIR = os.environ.get("OCCTL_TEST_DIR", "")


@unittest.skipUnless(SERVER and PASSWORD, "需要 OCCTL_TEST_SERVER / OCCTL_TEST_PASSWORD 才运行")
class RealServerTest(unittest.TestCase):
    session_id: str | None = None
    work_dir: str = ""

    @classmethod
    def setUpClass(cls):
        cls.work_dir = TEST_DIR or tempfile.mkdtemp(prefix="occtl-it-")

    @classmethod
    def tearDownClass(cls):
        if cls.session_id:
            cls.cli("rm", cls.session_id)

    @classmethod
    def cli(cls, *argv: str, timeout: float = 240.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(CLI), "--server", SERVER, "--password", PASSWORD, *argv],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def test_full_flow(self):
        # 1) 探活
        r = self.cli("--json", "info")
        self.assertEqual(r.returncode, 0, r.stderr)
        info = json.loads(r.stdout)
        self.assertTrue(info, "info 应返回内容")

        # 2) 建会话
        r = self.cli("--json", "new", "--dir", self.work_dir)
        self.assertEqual(r.returncode, 0, r.stderr)
        sid = json.loads(r.stdout)["id"]
        type(self).session_id = sid

        # 3) 派一轮并等到终态(流式 + SSE)
        r = self.cli("--json", "prompt", sid, "只回复两个字:ok", "--wait", "--timeout", "180")
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["status"], "succeeded")
        self.assertTrue((out.get("text") or "").strip(), f"应收到助手文本: {out}")

        # 4) 消息可读
        r = self.cli("--json", "messages", sid, "--limit", "5")
        self.assertEqual(r.returncode, 0, r.stderr)
        msgs = json.loads(r.stdout)
        self.assertTrue(any(m.get("type") == "assistant" for m in msgs), f"messages={msgs}")

        # 5) 机器视图可用
        r = self.cli("--json", "ps")
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = json.loads(r.stdout)
        self.assertTrue(rows and rows[0]["online"], f"ps={rows}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

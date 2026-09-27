import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bili_auth  # noqa: E402


class BiliAuthTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.config = Path(self.directory.name) / "config.json"
        self.config.write_text(json.dumps({"MCP": {"mcp_port": 1, "mcp_token": "t"}, "Cookie": {"is_login": False}}), encoding="utf-8")
        self.env = patch.dict("os.environ", {"BILI_CONFIG": str(self.config)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.directory.cleanup()

    def test_poll_states_and_success_writes_cookies(self):
        with patch.object(bili_auth, "_get", return_value={"data": {"code": 86090, "message": "scanned"}}):
            self.assertEqual(bili_auth.poll("abc123")["state"], "scanned")
        with self.assertRaises(ValueError):
            bili_auth.poll("../etc")
        success = {"data": {"code": 0, "url": "https://www.bilibili.com/?DedeUserID=42&SESSDATA=sess%2Cx&bili_jct=csrf"}}
        with patch.object(bili_auth, "_get", return_value=success):
            self.assertEqual(bili_auth.poll("abc123"), {"state": "success", "uid": "42"})
        cookie = json.loads(self.config.read_text(encoding="utf-8"))["Cookie"]
        self.assertEqual((cookie["SESSDATA"], cookie["bili_jct"], cookie["is_login"]), ("sess,x", "csrf", True))

        with patch.object(bili_auth, "_get", return_value={"data": {"isLogin": True, "uname": "nop", "vipStatus": 0}}), \
                patch.object(bili_auth.bili_pull, "call", return_value={"logged_in": False}):
            status = bili_auth.status()
        self.assertEqual((status["valid"], status["username"], status["bili23"]["logged_in"]), (True, "nop", False))
        self.assertNotIn("sess", json.dumps(status))  # cookies never reach the browser

        bili_auth.logout()
        cookie = json.loads(self.config.read_text(encoding="utf-8"))["Cookie"]
        self.assertEqual((cookie["SESSDATA"], cookie["is_login"]), ("", False))


if __name__ == "__main__":
    unittest.main()

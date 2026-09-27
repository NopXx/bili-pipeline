"""Bilibili QR-code login for the web UI (stdlib only).

The same web QR flow as bili_login.py, split into calls the browser can drive:
`start()` returns the URL to draw as a QR code, `poll()` reports scan progress
and, once the phone confirms, writes the session cookies into Bili23's
config.json. `status()` checks whether those cookies still work and whether the
running Bili23 has picked them up (it reads config.json at start-up, so a
restart is needed after a new login).

Cookies never leave the server: the API only returns the account name/uid.
"""
from http.cookiejar import CookieJar
import json
import os
import subprocess
import urllib.parse
import urllib.request

import bili_pull

GENERATE = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
NAV = "https://api.bilibili.com/x/web-interface/nav"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
COOKIE_KEYS = ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5")
# Poll codes from passport.bilibili.com
POLL_STATES = {0: "success", 86101: "waiting", 86090: "scanned", 86038: "expired"}


def _get(url, params=None, cookie_header=None, jar=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    headers = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
    if cookie_header:
        headers["Cookie"] = cookie_header
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar if jar is not None else CookieJar()))
    with opener.open(urllib.request.Request(url, headers=headers), timeout=15) as response:
        return json.loads(response.read())


def _config_path():
    path = bili_pull.find_config()
    if not path:
        raise RuntimeError("ไม่พบ config.json ของ Bili23 — เปิด Bili23 อย่างน้อยหนึ่งครั้ง หรือตั้ง BILI_CONFIG")
    return path


def _read_config(path):
    with open(path, encoding="utf-8") as source:
        return json.load(source)


def _write_config(path, config):
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as out:
        json.dump(config, out, ensure_ascii=False, indent=4)
    os.replace(temporary, path)


def start():
    data = _get(GENERATE)
    if data.get("code") != 0:
        raise RuntimeError(f"Bilibili สร้าง QR ไม่สำเร็จ: {data.get('message') or data.get('code')}")
    return {"url": data["data"]["url"], "key": data["data"]["qrcode_key"]}


def poll(key):
    if not key or len(key) > 64 or not key.isalnum():
        raise ValueError("invalid QR key")
    jar = CookieJar()
    data = _get(POLL, {"qrcode_key": key}, jar=jar).get("data") or {}
    state = POLL_STATES.get(data.get("code"), "error")
    if state != "success":
        return {"state": state, "message": data.get("message", "")}
    cookies = {cookie.name: cookie.value for cookie in jar if cookie.name in COOKIE_KEYS}
    # The redirect URL carries the same values; use it when a cookie is missing.
    query = urllib.parse.parse_qs(urllib.parse.urlparse(data.get("url", "")).query)
    for name in COOKIE_KEYS:
        if not cookies.get(name) and query.get(name):
            cookies[name] = query[name][0]
    if not cookies.get("SESSDATA"):
        raise RuntimeError("ล็อกอินสำเร็จแต่ไม่ได้รับ cookie SESSDATA")
    path = _config_path()
    config = _read_config(path)
    config.setdefault("Cookie", {}).update(cookies)
    config["Cookie"]["is_login"] = True
    _write_config(path, config)
    return {"state": "success", "uid": cookies.get("DedeUserID", "")}


def logout():
    path = _config_path()
    config = _read_config(path)
    cookie = config.setdefault("Cookie", {})
    for name in COOKIE_KEYS:
        cookie[name] = ""
    cookie["is_login"] = False
    _write_config(path, config)


def status():
    result = {"config": False, "saved": False, "valid": None, "bili23": None}
    path = bili_pull.find_config()
    if not path:
        return result
    result["config"] = True
    cookie = (_read_config(path).get("Cookie") or {})
    sessdata = cookie.get("SESSDATA") or ""
    result["saved"] = bool(sessdata)
    result["uid"] = cookie.get("DedeUserID") or ""
    if sessdata:
        try:
            nav = (_get(NAV, cookie_header=f"SESSDATA={sessdata}").get("data") or {})
            result["valid"] = bool(nav.get("isLogin"))
            if nav.get("isLogin"):
                result["username"] = nav.get("uname", "")
                result["vip"] = ((nav.get("vip_label") or {}).get("text") or "") if nav.get("vipStatus") else ""
        except Exception as exc:  # network trouble is not an invalid login
            result["check_error"] = str(exc)
    try:
        port, token = bili_pull.load_endpoint()
        login = bili_pull.call(port, token, "get_login_status", {})
        result["bili23"] = {"logged_in": bool(login.get("logged_in")), "username": login.get("username") or ""}
    except SystemExit as exc:  # bili_pull reports MCP trouble with sys.exit
        result["bili23_error"] = str(exc)
    return result


def restart_bili23():
    command = os.environ.get("BILI23_RESTART_CMD", "systemctl restart bili23.service")
    result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=60)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or f"exit code {result.returncode}").strip()[-600:])

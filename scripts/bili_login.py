#!/usr/bin/env python3
"""
Log Bili23 into Bilibili on a headless box, no browser: show a QR in the
terminal, you scan it with the Bilibili phone app, and the resulting cookies
are written straight into Bili23's config.json. Restart the bili23 service after.

    /opt/bili23/.venv/bin/python bili_login.py     # uses httpx + qrcode from the venv

Bilibili's own web QR-login flow — same one the GUI uses, just drawn as ASCII.
"""
import json
import os
import sys
import time

import httpx
import qrcode

GEN = "https://passport.bilibili.com/x/passport-login/web/qrcode/generate"
POLL = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def config_path():
    if p := os.environ.get("BILI_CONFIG"):
        return p
    home = os.path.expanduser("~")
    for c in (
        f"{home}/.local/share/Bili23 Downloader/config.json",
        f"{home}/.config/Bili23 Downloader/config.json",
    ):
        if os.path.exists(c):
            return c
    sys.exit("config.json not found — start Bili23 once first, or set BILI_CONFIG")


def main():
    with httpx.Client(headers={"User-Agent": UA}, timeout=15) as client:
        gen = client.get(GEN).json()
        if gen.get("code") != 0:
            sys.exit(f"generate failed: {gen}")
        url = gen["data"]["url"]
        key = gen["data"]["qrcode_key"]

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make()
        qr.print_ascii(invert=True)
        print("\nScan with the Bilibili phone app (Mine → scan). If the QR is unclear, open this:")
        print(url + "\n")

        while True:
            time.sleep(2)
            data = client.get(POLL, params={"qrcode_key": key}).json()["data"]
            code = data["code"]
            if code == 0:
                break
            if code == 86038:
                sys.exit("QR expired — rerun the script")
            if code == 86090:
                print("scanned — confirm on your phone…", flush=True)
            # 86101 = waiting for scan; keep polling

        jar = client.cookies
        cookies = {
            "SESSDATA": jar.get("SESSDATA", ""),
            "bili_jct": jar.get("bili_jct", ""),
            "DedeUserID": jar.get("DedeUserID", ""),
            "DedeUserID__ckMd5": jar.get("DedeUserID__ckMd5", ""),
        }
        if not cookies["SESSDATA"]:
            sys.exit(f"login succeeded but no SESSDATA cookie? {dict(jar)}")

    path = config_path()
    cfg = json.load(open(path, encoding="utf-8"))
    cfg.setdefault("Cookie", {})
    cfg["Cookie"].update(cookies)
    cfg["Cookie"]["is_login"] = True
    json.dump(cfg, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=4)

    print(f"\n✓ logged in as uid {cookies['DedeUserID']} — cookies written to config.json")
    print("Now restart Bili23:  systemctl restart bili23")


if __name__ == "__main__":
    main()

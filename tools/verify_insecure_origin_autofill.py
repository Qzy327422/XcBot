# -*- coding: utf-8 -*-
"""实测：非安全上下文（等价于公网 http://IP 访问）下登录页的自动填充能力。

为什么要单独测：浏览器把 `localhost` / `127.0.0.1` 当安全上下文，把
`http://公网IP` 当不安全。凭据管理 API（PasswordCredential）只在安全上下文存在，
所以本机测通不代表公网也通。

怎么不暴露服务还能测：服务照旧只绑 127.0.0.1，但给 Chromium 加
--host-resolver-rules，把一个假域名解析到 127.0.0.1。页面 origin 变成
http://insecure.test（非 localhost、非 HTTPS = 不安全上下文），
服务端一个字节都没多监听。

用法：python tools/verify_insecure_origin_autofill.py
"""
from __future__ import annotations

import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

results: "list[tuple[str, str, str]]" = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    results.append((status, name, detail))
    print(f"[{status}] {name}" + (f" - {detail}" if detail else ""))


def info(name: str, value: str) -> None:
    """只记录事实、不判成败——浏览器策略不由我们决定。"""
    print(f"[INFO] {name}: {value}")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


CTX_JS = """()=>({
  origin:location.origin,
  secure:window.isSecureContext,
  hasCredentials:!!navigator.credentials,
  hasStore:!!(navigator.credentials&&navigator.credentials.store),
  hasPasswordCredential:typeof window.PasswordCredential!=='undefined',
  hasRememberCred:typeof rememberCred==='function'
})"""


def main() -> int:
    import webui
    from playwright.sync_api import sync_playwright

    token = "verify-insecure-token"
    orig = webui.get_webui_config

    def patched():
        cfg = dict(orig())
        cfg["access_token"] = token
        cfg["enabled"] = True
        return cfg

    webui.get_webui_config = patched
    port = free_port()
    srv = webui.start_webui(host="127.0.0.1", port=port)
    if srv is None:
        ok("WebUI 起得来", False, "已有实例？")
        webui.get_webui_config = orig
        return 1
    time.sleep(0.6)

    fake_host = "insecure.test"
    secure_base = f"http://127.0.0.1:{port}"
    insecure_base = f"http://{fake_host}:{port}"

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=[
                f"--host-resolver-rules=MAP {fake_host} 127.0.0.1",
                # 本机环境里有 ALL_PROXY / HTTPS_PROXY 指向本地代理，而 NO_PROXY 只列了
                # localhost/127.0.0.1，假域名不在其中，Chromium 会把请求交给那个代理，
                # 代理连不上就回 502（页面空白）。这两个参数才真正让它直连；
                # 单靠 --no-proxy-server 或改环境变量都无效，实测过。
                "--proxy-server=direct://",
                "--proxy-bypass-list=*",
            ])

            # ---- A) 安全上下文（127.0.0.1）作为对照 ----
            pg = browser.new_page()
            errs: "list[str]" = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(secure_base + "/auth/login", wait_until="load")
            pg.wait_for_timeout(300)
            a = pg.evaluate(CTX_JS)
            ok("对照组：127.0.0.1 是安全上下文", a["secure"] is True, f"{a['origin']}")
            ok("对照组：凭据管理 API 可用", a["hasPasswordCredential"] and a["hasStore"],
               f"PasswordCredential={a['hasPasswordCredential']} store={a['hasStore']}")
            ok("对照组：登录仍然成功", _login(pg, token, secure_base),
               pg.url.replace(secure_base, "") or "/")
            ok("对照组无 JS 错误", not errs, f"{errs[:3]}")
            pg.close()

            # ---- B) 非安全上下文（假域名，等价公网 http://IP）----
            pg2 = browser.new_page()
            errs2: "list[str]" = []
            pg2.on("pageerror", lambda e: errs2.append(str(e)))
            resp2 = pg2.goto(insecure_base + "/auth/login", wait_until="load")
            pg2.wait_for_timeout(300)
            ok("实验组：登录页正常返回 200",
               resp2 is not None and resp2.status == 200,
               f"status={resp2.status if resp2 else 'none'} url={pg2.url} "
               f"body={(pg2.content() or '')[:120]!r}")
            b = pg2.evaluate(CTX_JS)
            ok("实验组：非 localhost 的 http 不是安全上下文",
               b["secure"] is False, f"{b['origin']} secure={b['secure']}")
            info("实验组 PasswordCredential 是否存在", str(b["hasPasswordCredential"]))
            info("实验组 navigator.credentials.store 是否存在", str(b["hasStore"]))
            ok("rememberCred 仍然定义", b["hasRememberCred"] is True)

            # 关键：接口缺失时不能抛错、不能拦住登录
            ok("实验组：登录照样成功", _login(pg2, token, insecure_base),
               pg2.url.replace(insecure_base, "") or "/")
            ok("实验组：接口缺失也不报 JS 错误", not errs2, f"{errs2[:3]}")
            ok("实验组：Token 正常写入 localStorage",
               pg2.evaluate("localStorage.webuiToken||''") == token)

            # 直接调一次 rememberCred，确认它在缺接口时静默返回而不是 reject
            pg2.goto(insecure_base + "/auth/login", wait_until="load")
            pg2.wait_for_timeout(200)
            thrown = pg2.evaluate(
                "async()=>{try{await rememberCred('x');return ''}"
                "catch(e){return String(e&&e.message||e)}}")
            ok("rememberCred 在非安全上下文静默跳过", thrown == "", repr(thrown))

            # 表单结构与安全上下文完全一致
            form = pg2.evaluate(
                "()=>{const f=document.querySelector('form'),t=document.getElementById('tok');"
                "return {method:(f.getAttribute('method')||'').toLowerCase(),"
                "names:[...f.elements].filter(e=>e.name).map(e=>e.name),"
                "tAuto:t.autocomplete,tName:t.name}}")
            ok("实验组：表单结构与安全上下文一致",
               form["method"] == "post" and form["names"] == ["password"]
               and form["tName"] == "password"
               and form["tAuto"] == "current-password",
               f"{form}")
            pg2.close()
            browser.close()
    finally:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
        webui.get_webui_config = orig
        try:
            webui._login_fails.clear()
        except Exception:
            pass

    fails = [r for r in results if r[0] == "FAIL"]
    print("\n=== SUMMARY ===")
    print(f"total={len(results)} pass={len(results) - len(fails)} fail={len(fails)}")
    if fails:
        print("FAILURES:")
        for _, name, detail in fails:
            print(f"  - {name}: {detail}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


def _login(page, token: str, base: str) -> bool:
    page.fill("#tok", token)
    page.click("#btn")
    page.wait_for_timeout(1500)
    return page.url.split("#")[0].rstrip("/") == base


if __name__ == "__main__":
    raise SystemExit(main())

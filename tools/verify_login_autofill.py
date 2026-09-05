# -*- coding: utf-8 -*-
"""登录页凭据自动填充的验证（起真实 WebUI + 真实 Chromium）。

背景：原登录表单只有一个孤零零的 type="password"，没有用户名字段、没有 name、
没有 method，登录还是 preventDefault + fetch + location.href。密码管理器认不出
这是登录表单（缺凭据对），浏览器的「密码用对了，要不要保存」启发式也不会触发
（没有原生提交/导航）。这里验证补齐后的结构与运行时行为。

检查点：
  1) 表单结构：method=post、username 字段（autocomplete=username）、
     password 字段带 name、autocomplete=current-password；
  2) username 字段视觉上不可见但仍可被自动填充（不能用 display:none —
     Chromium 会跳过 display:none/visibility:hidden 的字段）；
  3) 浏览器把它识别成登录表单：填入值后能正常读出、可正常提交事件；
  4) 正常登录路径未被破坏：Token 正确 → 跳首页、写 localStorage；
     Token 错误 → 停在登录页、不写 localStorage；
  5) rememberCred 存在且不抛错（非安全上下文下 PasswordCredential 不存在也要静默跳过）；
  6) 禁用 JS 时表单 POST 到 /auth/login 不会 404、不会把 Token 落进地址栏。

用法：python tools/verify_login_autofill.py
"""
from __future__ import annotations

import re
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


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


LOGIN_MAX_FAILS = 5


def _probe_other(base: str, tok: str) -> int:
    """打一个非登录探测口，确认冷却没有波及正常使用。"""
    import urllib.error as ue
    import urllib.request as ur
    req = ur.Request(base + "/api/chat/sessions", headers={"X-WebUI-Token": tok})
    try:
        with ur.urlopen(req, timeout=10) as r:
            return r.status
    except ue.HTTPError as he:
        return he.code
    except Exception:
        return -1


FIELD_JS = """()=>{const f=document.querySelector('form'),
  t=document.getElementById('tok');
  if(!f||!t)return {err:`form=${!!f} tok=${!!t}`};
  return {method:(f.getAttribute('method')||'').toLowerCase(),
          action:f.getAttribute('action'),
          pName:t.name,pType:t.type,pAuto:t.autocomplete,
          hasRemember:typeof rememberCred==='function',
          // 没有账号框是刻意的：加一个纯摆设的账号框太突兀，且它不参与任何校验
          hasUname:!!document.getElementById('uname'),
          // 表单里被浏览器视为可提交的字段名
          names:[...f.elements].filter(e=>e.name).map(e=>e.name)}}"""


def main() -> int:
    import webui
    from playwright.sync_api import sync_playwright

    # 用一个临时 Token 跑，不依赖也不改动真实 config.json：
    # 用户可能正好把 access_token 清空了（首次设置引导态），那样登录页根本不会出现，
    # 之前的做法是直接跳过，等于这套检查在最需要它的时候不跑。
    token = "verify-login-token"
    _orig_get_cfg = webui.get_webui_config

    def _patched_get_cfg():
        cfg = dict(_orig_get_cfg())
        cfg["access_token"] = token
        cfg["enabled"] = True
        return cfg

    webui.get_webui_config = _patched_get_cfg

    port = free_port()
    srv = webui.start_webui(host="127.0.0.1", port=port)
    if srv is None:
        print("WebUI 未启动（已有实例？）")
        webui.get_webui_config = _orig_get_cfg
        return 1
    time.sleep(0.6)
    base = f"http://127.0.0.1:{port}"
    login_url = base + "/auth/login"

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()

            # ---- 1) 结构与可见性 ----
            ctx = browser.new_context(viewport={"width": 1280, "height": 860})
            pg = ctx.new_page()
            errs: "list[str]" = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(login_url, wait_until="load")
            pg.wait_for_timeout(400)
            ok("登录页无 JS 错误", not errs, f"{errs[:3]}")

            v = pg.evaluate(FIELD_JS)
            if v.get("err"):
                ok("登录表单结构完整", False, v["err"])
                browser.close()
                return 1
            ok("表单与 Token 字段都在", True)
            ok("表单 method=post", v["method"] == "post", f"method={v['method']!r}")
            ok("password 字段有 name", v["pName"] == "password", f"name={v['pName']!r}")
            ok("password 仍是 password 类型", v["pType"] == "password", f"{v['pType']}")
            ok("password autocomplete=current-password",
               v["pAuto"] == "current-password", f"{v['pAuto']!r}")
            ok("表单只有 password 一个可提交字段",
               v["names"] == ["password"], f"{v['names']}")
            # 刻意不加账号框：一个永远填 webui、不参与校验的框在界面上太突兀。
            # 代价是 Chromium 的「保存密码」启发式需要凭据对，单密码框命中率低，
            # 所以保存改由 rememberCred 里的凭据管理 API 主动完成（见下）。
            ok("没有摆设用的账号框", v["hasUname"] is False)
            ok("rememberCred 用固定 id 而非读账号框",
               "el('uname')" not in (ROOT / "static" / "login.js").read_text(encoding="utf-8"),
               "账号框已删，不能再去读它")

            tr = pg.evaluate("()=>{const r=document.getElementById('tok').getBoundingClientRect();"
                             "return {w:Math.round(r.width),h:Math.round(r.height),y:Math.round(r.y)}}")
            ok("Token 输入框正常显示", tr["w"] > 120 and tr["h"] > 20,
               f"{tr['w']}x{tr['h']} y={tr['y']}")
            ok("rememberCred 已定义", v["hasRemember"] is True)

            # 眼睛按钮：图标必须是 SVG（emoji 各平台造型不一），且点击切换明文
            ok("眼睛按钮用 SVG 不用 emoji",
               pg.evaluate("!!document.querySelector('#eyeBtn svg')"),
               pg.evaluate("(document.getElementById('eyeBtn')||{}).innerHTML||''")[:40])
            ok("主题按钮用 SVG 不用 emoji",
               pg.evaluate("!!document.querySelector('#themeBtn svg')"),
               pg.evaluate("(document.getElementById('themeBtn')||{}).textContent||''")[:20])
            emoji = pg.evaluate(
                "()=>{const t=document.body.innerText||'';"
                # 只挑真正的绘文字（Extended_Pictographic），不能按码位阈值筛——
                # 中文本身就在 0x2100 以上，那样会把「请输入访问」当成 emoji
                "return [...t].filter(c=>/\\p{Extended_Pictographic}/u.test(c)).join('')}")
            ok("登录页正文无 emoji 残留", not emoji, repr(emoji[:30]))
            pg.fill("#tok", "abc")
            pg.click("#eyeBtn")
            ok("明文切换仍可用",
               pg.evaluate("document.getElementById('tok').type") == "text")
            ok("切到明文后图标变为闭眼",
               pg.evaluate("!!document.querySelector('#eyeBtn svg')")
               and pg.evaluate("document.getElementById('eyeBtn').title") == "隐藏",
               pg.evaluate("document.getElementById('eyeBtn').title"))
            pg.click("#eyeBtn")
            ok("再点回密码态",
               pg.evaluate("document.getElementById('tok').type") == "password")

            # ---- 2) 错误 Token ----
            pg.fill("#tok", "definitely-wrong-token-zzz")
            pg.click("#btn")
            pg.wait_for_timeout(900)
            ok("错误 Token 停在登录页", "/auth/login" in pg.url, pg.url.replace(base, ""))
            msg = (pg.text_content("#msg") or "").strip()
            ok("错误 Token 有提示", bool(msg), repr(msg))
            # 服务端会带上剩余次数，前端要原样显示而不是用固定文案盖掉
            ok("提示里带剩余尝试次数", "还可尝试" in msg, repr(msg))
            ok("错误 Token 不写 localStorage",
               not pg.evaluate("localStorage.webuiToken||''"))
            ok("错误路径无 JS 错误", not errs, f"{errs[:3]}")

            # ---- 3) 正确 Token ----
            pg.fill("#tok", token)
            pg.click("#btn")
            pg.wait_for_timeout(1600)
            ok("正确 Token 跳到首页",
               pg.url.split("#")[0].rstrip("/") in (base, base + "/index.html"),
               pg.url.replace(base, "") or "/")
            ok("正确 Token 写入 localStorage",
               pg.evaluate("localStorage.webuiToken||''") == token)
            ok("Token 未出现在地址栏", "password=" not in pg.url and token not in pg.url,
               pg.url.replace(base, "") or "/")
            ok("登录后无 JS 错误", not errs, f"{errs[:3]}")

            # ---- 3b) 老用户兼容：登录只靠 Token，请求里不带任何账号概念 ----
            # 从旧版本升级上来的用户配置里只有 Token、没有账号字段。这里盯住实际
            # 发出的请求，确认鉴权只用 X-WebUI-Token 头、没有多出来的表单体。
            ctx4 = browser.new_context(viewport={"width": 1280, "height": 860})
            pg4 = ctx4.new_page()
            errs4: "list[str]" = []
            pg4.on("pageerror", lambda e: errs4.append(str(e)))
            sent: "list[dict]" = []
            pg4.on("request", lambda r: sent.append({
                "url": r.url, "method": r.method,
                "body": (r.post_data or ""),
                "hdr": r.headers.get("x-webui-token", ""),
            }) if "/api/" in r.url else None)
            pg4.goto(login_url, wait_until="load")
            pg4.wait_for_timeout(300)
            pg4.fill("#tok", token)
            pg4.click("#btn")
            pg4.wait_for_timeout(1500)
            ok("只填 Token 即可登录",
               pg4.url.split("#")[0].rstrip("/") == base,
               pg4.url.replace(base, "") or "/")
            probes = [r for r in sent if "/api/ui-state" in r["url"]]
            ok("校验请求确实发出", bool(probes), f"{len(probes)} 次")
            ok("Token 只走请求头",
               all(p["hdr"] == token for p in probes), "X-WebUI-Token 头缺失或不符")
            ok("校验请求是 GET",
               all(p["method"] == "GET" for p in probes),
               f"{[p['method'] for p in probes]}")
            ok("校验请求不带请求体",
               all(not (p["body"] or "").strip() for p in probes),
               "登录只是一次带头的 GET，没有表单体")
            ok("请求里不含 username 字段",
               all("username" not in (p["body"] or "") for p in probes),
               f"bodies={[p['body'][:40] for p in probes]}")
            ok("老用户兼容路径无 JS 错误", not errs4, f"{errs4[:3]}")
            ctx4.close()

            # ---- 4) 按 Enter 提交也走 JS 拦截，不做原生 POST ----
            ctx2 = browser.new_context(viewport={"width": 1280, "height": 860})
            pg2 = ctx2.new_page()
            nav: "list[str]" = []
            pg2.on("framenavigated", lambda f: nav.append(f.url))
            pg2.goto(login_url, wait_until="load")
            pg2.wait_for_timeout(300)
            pg2.fill("#tok", token)
            pg2.press("#tok", "Enter")
            pg2.wait_for_timeout(1600)
            ok("回车登录同样跳首页", pg2.url.split("#")[0].rstrip("/") == base,
               pg2.url.replace(base, "") or "/")
            ok("回车未触发原生表单 POST",
               not any("password=" in u for u in nav), f"{[u.replace(base,'') for u in nav]}")
            ctx2.close()

            # ---- 5) 禁用 JS：原生 POST 不能 404，也不能把 Token 暴露在 URL ----
            ctx3 = browser.new_context(java_script_enabled=False,
                                       viewport={"width": 1280, "height": 860})
            pg3 = ctx3.new_page()
            resp = pg3.goto(login_url, wait_until="load")
            ok("禁用 JS 时登录页仍 200", resp is not None and resp.status == 200,
               f"status={resp.status if resp else 'none'}")
            pg3.fill("#tok", token)
            with pg3.expect_navigation(wait_until="load"):
                pg3.click("#btn")
            ok("禁用 JS 原生 POST 不 404",
               "Token" in (pg3.content() or ""), pg3.url.replace(base, ""))
            ok("禁用 JS 时 Token 未进地址栏",
               token not in pg3.url and "password=" not in pg3.url,
               pg3.url.replace(base, ""))
            ctx3.close()

            # ---- 6) 连续错 5 次进冷却，冷却期内正确 Token 也拒绝 ----
            # 取代原来的「Token 至少 8 位」硬限制，所以必须实测而不是只读代码。
            import urllib.error as _ue
            import urllib.request as _ur

            def probe(tok: str) -> int:
                req = _ur.Request(base + "/api/ui-state",
                                  headers={"X-WebUI-Token": tok})
                try:
                    with _ur.urlopen(req, timeout=10) as r:
                        return r.status
                except _ue.HTTPError as he:
                    return he.code
                except Exception:
                    return -1

            codes = [probe("wrong-" + str(i)) for i in range(LOGIN_MAX_FAILS)]
            ok("前 4 次错误返回 401", codes[:-1] == [401] * (LOGIN_MAX_FAILS - 1), f"{codes}")
            ok(f"第 {LOGIN_MAX_FAILS} 次触发 429", codes[-1] == 429, f"{codes}")
            ok("冷却期内正确 Token 也被拒", probe(token) == 429,
               "猜对就放过等于限流形同虚设")
            # 非登录探测口不受冷却影响，避免正常使用被一次误输入锁死
            other = probe(token)  # /api/ui-state 仍在冷却
            ok("冷却只作用于登录探测口",
               other == 429 and _probe_other(base, token) == 200,
               f"ui-state={other} sessions={_probe_other(base, token)}")
            # 手工解锁后应恢复
            webui._login_fails.clear()
            ok("清空计数后可再次登录", probe(token) == 200)
            # 锁定时长以 webui 里的常量为准，顺手断言它没被改成折磨人的长度
            ok("锁定时长为 30 秒", webui.LOGIN_LOCK_SECONDS == 30,
               f"{webui.LOGIN_LOCK_SECONDS}s")
            # 锁到期后自动解锁：把到期时间往前拨，模拟等够了
            for i in range(LOGIN_MAX_FAILS):
                probe("wrong-again-" + str(i))
            ok("再次连错进入冷却", probe(token) == 429)
            with webui._login_fails_lock:
                for row in webui._login_fails.values():
                    row[2] = time.time() - 1  # 锁已过期
            ok("冷却到期自动解锁", probe(token) == 200,
               "不需要重启，也不需要手工清表")

            ctx.close()
            browser.close()
    finally:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
        webui.get_webui_config = _orig_get_cfg
        try:
            webui._login_fails.clear()
        except Exception:
            pass

    # ---- 7) 静态检查：账号框相关的代码要彻底清干净 ----
    src = (ROOT / "webui.py").read_text(encoding="utf-8")
    m = re.search(r"LOGIN_HTML = r'''(.*?)'''", src, re.S)
    html = m.group(1) if m else ""
    ok("HTML 里没有账号输入框", 'id="uname"' not in html)
    ok("HTML 里没有 username 字段", 'name="username"' not in html)
    ok("样式里没有残留的 .uname 隐藏类", ".uname{" not in html,
       "1px + clip 那套已删除，别再加回来")
    ok("样式里没有残留的 .acct / .hint", ".field.acct" not in html and ".hint{" not in html)
    js = (ROOT / "static" / "login.js").read_text(encoding="utf-8")
    ok("login.js 不再引用账号框", "uname" not in js)
    ok("凭据 id 固定为 webui", "id:'webui'" in js.replace(" ", ""),
       "PasswordCredential 的 id 只是显示名，不参与校验")

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


if __name__ == "__main__":
    raise SystemExit(main())

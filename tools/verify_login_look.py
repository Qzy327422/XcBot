# -*- coding: utf-8 -*-
"""登录页外观与主界面一致性验证（Playwright + 真实服务）。

背景：登录页原先写死一套青蓝配色，主界面按 WebUI.theme_preset 换色、还能贴
自定义背景图，两边看起来是两个产品。要让登录页跟上，又不能为它开一个匿名可
访问的接口（登录前多一个数据出口）。做法是主界面登录后把外观缓存进
localStorage（背景图缩小后转 data URL），登录页只读本地缓存。

检查点：
  1) 登录页认 data-webui-preset，7 套预设各自生效、且与 app.css 同名规则同色；
  2) 有缓存背景图时铺满、加 has-bg、卡片压暗；无缓存时退回渐变色且不报错；
  3) 缓存里的 bg 只接受 data:/http(s):，别的形态忽略（防 CSS url() 注入）；
  4) 抖动动画收敛且不破坏卡片的 translateY 定位；
  5) 登录页不发起任何未鉴权的后端请求（只允许 HTML / login.js / icon）；
  6) 主界面 applyWebuiAppearance 会写缓存，且不会把超大图塞进 localStorage。

用法：python tools/verify_login_look.py
"""
from __future__ import annotations

import json
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


PRESETS = ["aurora", "xcbot", "midnight", "sakura", "forest", "sunset", "ocean"]

LOOK_JS = """()=>{
  const root=document.documentElement,cs=getComputedStyle(root);
  const card=document.querySelector('.login');
  const bcs=getComputedStyle(document.body);
  const btn=document.getElementById('btn');
  return {preset:root.dataset.webuiPreset||'',
          bg0:cs.getPropertyValue('--bg0').trim(),
          accent:cs.getPropertyValue('--accent').trim(),
          bgImg:cs.getPropertyValue('--user-bg-image').trim(),
          blur:cs.getPropertyValue('--user-bg-blur').trim(),
          hasBg:root.classList.contains('has-bg'),
          cardBg:card?getComputedStyle(card).backgroundImage.slice(0,60):'',
          cardY:Math.round(card?card.getBoundingClientRect().y:-1),
          btnColor:btn?getComputedStyle(btn).color:'',
          btnBg:btn?getComputedStyle(btn).backgroundImage:'',
          btnWeight:btn?getComputedStyle(btn).fontWeight:'',
          bodyBg:bcs.backgroundImage.slice(0,40)}}"""

# 主界面上取同样的项，用来逐项对齐
MAIN_JS = """()=>{
  const root=document.documentElement,cs=getComputedStyle(root);
  const btn=document.querySelector('.btn.primary');
  const vars={};
  for(const k of ['--bg0','--bg1','--accent','--accent2','--accent-rgb','--accent2-rgb',
                  '--text','--glass','--glass2','--line'])
    vars[k]=cs.getPropertyValue(k).trim();
  return {vars:vars,
          btnColor:btn?getComputedStyle(btn).color:'',
          btnBg:btn?getComputedStyle(btn).backgroundImage:'',
          btnWeight:btn?getComputedStyle(btn).fontWeight:''}}"""

# 登录页上取同一批变量
VARS_JS = """()=>{
  const cs=getComputedStyle(document.documentElement),vars={};
  for(const k of ['--bg0','--bg1','--accent','--accent2','--accent-rgb','--accent2-rgb',
                  '--text','--glass','--glass2','--line'])
    vars[k]=cs.getPropertyValue(k).trim();
  return vars}"""

# 把 body 的直系子节点全隐掉，只留背景层，才能逐像素比背景。
# .app 带 display:grid !important，普通 style.display 盖不住，必须给 important。
HIDE_JS = """()=>{document.querySelectorAll('body>*').forEach(c=>
  c.style.setProperty('display','none','important'))}"""

SAMPLE_PTS = [(60, 60), (1380, 60), (60, 840), (1380, 840),
              (720, 90), (720, 860), (40, 450), (1400, 450), (720, 450)]

# 1x1 红色 png，够用来验证「有图」这条路径
TINY_PNG = ("data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
            "AAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")


def css_preset_colors() -> "dict[str, dict[str, str]]":
    """从 app.css 抽出各预设的 --bg0 / --accent，作为登录页的比对基准。"""
    css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
    out: "dict[str, dict[str, str]]" = {}
    for p in PRESETS:
        m = re.search(r'html:not\(\[data-theme="light"\]\)\[data-webui-preset="'
                      + p + r'"\]\{([^}]*)\}', css)
        if not m:
            continue
        body = m.group(1)
        d = {}
        for key in ("--bg0", "--accent"):
            mm = re.search(re.escape(key) + r":([^;}]+)", body)
            if mm:
                d[key] = mm.group(1).strip()
        out[p] = d
    return out


def main() -> int:
    import webui
    from playwright.sync_api import sync_playwright

    token = "verify-look-token"
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
    base = f"http://127.0.0.1:{port}"
    login_url = base + "/auth/login"

    ref = css_preset_colors()
    ok("从 app.css 取到预设基准色", len(ref) == len(PRESETS), f"{sorted(ref)}")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()

            # ---- 0) 深浅两种模式下，逐像素比背景 + 逐项比变量与按钮 ----
            # 这是本脚本最硬的一条：变量对上不代表画面一样，最终纹理必须实测。
            shots = ROOT / "temps_lookshots"
            shots.mkdir(exist_ok=True)
            cmp_ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            for theme in ("light", "dark"):
                pm = cmp_ctx.new_page()
                pm.goto(f"{base}/?token={token}", wait_until="load")
                pm.wait_for_selector("#content .card", timeout=15000)
                pm.evaluate(f"()=>setTheme('{theme}')")
                pm.wait_for_timeout(700)
                mv = pm.evaluate(MAIN_JS)
                pm.evaluate(HIDE_JS)
                pm.wait_for_timeout(280)
                pm.screenshot(path=str(shots / f"main_{theme}.png"))
                pm.close()

                pl = cmp_ctx.new_page()
                pl.goto(login_url, wait_until="load")
                pl.evaluate(f"()=>setTheme('{theme}')")
                pl.wait_for_timeout(420)
                lvars = pl.evaluate(VARS_JS)
                lv = pl.evaluate(LOOK_JS)
                pl.evaluate(HIDE_JS)
                pl.wait_for_timeout(280)
                pl.screenshot(path=str(shots / f"login_{theme}.png"))
                pl.close()

                diff_vars = {k: (mv["vars"][k], lvars.get(k))
                             for k in mv["vars"] if mv["vars"][k] != lvars.get(k)}
                ok(f"[{theme}] CSS 变量与主界面逐项一致", not diff_vars, f"{diff_vars}")
                ok(f"[{theme}] 登录按钮配色与主界面一致",
                   lv["btnBg"] == mv["btnBg"], f"login={lv['btnBg'][:70]} main={mv['btnBg'][:70]}")
                ok(f"[{theme}] 登录按钮文字色跟随主题",
                   lv["btnColor"] == mv["btnColor"],
                   f"login={lv['btnColor']} main={mv['btnColor']}")
                ok(f"[{theme}] 登录按钮字重与主界面一致",
                   lv["btnWeight"] == mv["btnWeight"],
                   f"login={lv['btnWeight']} main={mv['btnWeight']}")

                from PIL import Image
                im_m = Image.open(shots / f"main_{theme}.png").convert("RGB")
                im_l = Image.open(shots / f"login_{theme}.png").convert("RGB")
                worst, spots = 0, []
                for (x, y) in SAMPLE_PTS:
                    a, b = im_m.getpixel((x, y)), im_l.getpixel((x, y))
                    d = max(abs(a[i] - b[i]) for i in range(3))
                    if d > worst:
                        worst = d
                    if d > 4:
                        spots.append(f"({x},{y})Δ{d} main={a} login={b}")
                ok(f"[{theme}] 背景逐像素与主界面一致（9 个采样点）",
                   worst <= 4, f"最大Δ={worst} {spots[:3]}")
            cmp_ctx.close()
            try:
                for f in shots.iterdir():
                    f.unlink()
                shots.rmdir()
            except Exception:
                pass

            # ---- 5) 登录页发了哪些请求 ----
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})
            pg = ctx.new_page()
            errs: "list[str]" = []
            reqs: "list[str]" = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("request", lambda r: reqs.append(r.url.replace(base, "")))
            pg.goto(login_url, wait_until="load")
            pg.wait_for_timeout(500)
            ok("登录页无 JS 错误", not errs, f"{errs[:3]}")
            api_calls = [u for u in reqs if u.startswith("/api/")]
            ok("登录页不请求任何 /api/ 接口", not api_calls, f"{api_calls}")
            allowed = ("/auth/login", "/static/login.js", "/assets/icon.jpg", "/favicon.ico")
            unexpected = [u for u in reqs
                          if not any(u.split("?")[0] == a for a in allowed)]
            ok("登录页只取 HTML/JS/图标", not unexpected, f"{unexpected}")

            # ---- 2b) 无缓存时退回渐变色 ----
            v = pg.evaluate(LOOK_JS)
            ok("无缓存不加 has-bg", v["hasBg"] is False)
            ok("无缓存时 body 仍有渐变背景",
               "gradient" in v["bodyBg"], f"{v['bodyBg']!r}")
            ok("卡片没被顶到视口外", 0 < v["cardY"] < 900, f"y={v['cardY']}")

            # ---- 1) 七套预设逐个验色 ----
            for p in PRESETS:
                pg.evaluate(
                    "(o)=>localStorage.setItem('xcbotLoginLook',JSON.stringify(o))",
                    {"preset": p, "blur": 10},
                )
                pg.reload(wait_until="load")
                pg.wait_for_timeout(260)
                v = pg.evaluate(LOOK_JS)
                ok(f"预设 {p} 已应用", v["preset"] == p, f"{v['preset']!r}")
                exp = ref.get(p, {})
                ok(f"预设 {p} 的 --bg0 与主界面一致",
                   v["bg0"].lower() == exp.get("--bg0", "").lower(),
                   f"login={v['bg0']} app={exp.get('--bg0')}")
                ok(f"预设 {p} 的 --accent 与主界面一致",
                   v["accent"].lower() == exp.get("--accent", "").lower(),
                   f"login={v['accent']} app={exp.get('--accent')}")

            # ---- 2) 有缓存背景图 ----
            pg.evaluate(
                "(o)=>localStorage.setItem('xcbotLoginLook',JSON.stringify(o))",
                {"preset": "sakura", "blur": 14, "src": "data/webui_bg/x.png", "bg": TINY_PNG},
            )
            pg.reload(wait_until="load")
            pg.wait_for_timeout(320)
            v = pg.evaluate(LOOK_JS)
            ok("有缓存图时加上 has-bg", v["hasBg"] is True)
            ok("背景图写进 --user-bg-image",
               "data:image/png" in v["bgImg"], v["bgImg"][:48])
            ok("模糊度跟随缓存", v["blur"] == "14px", f"{v['blur']!r}")
            ok("有图时卡片改用压暗底色",
               "rgba(10, 20, 30" in v["cardBg"] or "rgba(10,20,30" in v["cardBg"],
               v["cardBg"])
            ok("背景层铺满视口", pg.evaluate(
                "()=>{const r=getComputedStyle(document.body,'::before');"
                "return r.backgroundSize==='cover'&&r.position==='fixed'}"))
            ok("有图时仍无 JS 错误", not errs, f"{errs[:3]}")

            # ---- 3) 非法 bg 形态必须被忽略 ----
            for bad in ("javascript:alert(1)", 'x");background:url(evil', "//evil.test/a.png"):
                pg.evaluate(
                    "(o)=>localStorage.setItem('xcbotLoginLook',JSON.stringify(o))",
                    {"preset": "aurora", "blur": 0, "src": "a", "bg": bad},
                )
                pg.reload(wait_until="load")
                pg.wait_for_timeout(220)
                v = pg.evaluate(LOOK_JS)
                ok(f"忽略非法背景值 {bad[:18]!r}",
                   v["hasBg"] is False and "url(" not in v["bgImg"],
                   f"hasBg={v['hasBg']} img={v['bgImg'][:40]}")
            ok("非法值路径无 JS 错误", not errs, f"{errs[:3]}")

            # ---- 4) 抖动动画 ----
            pg.evaluate("()=>localStorage.removeItem('xcbotLoginLook')")
            pg.reload(wait_until="load")
            pg.wait_for_timeout(250)
            anim = pg.evaluate(
                "()=>{const c=document.querySelector('.login');"
                "const before=c.getBoundingClientRect().y;"
                "c.classList.add('shake');const s=getComputedStyle(c);"
                "return {dur:s.animationDuration,cnt:s.animationIterationCount,"
                "name:s.animationName,y:Math.round(c.getBoundingClientRect().y),before:Math.round(before)}}")
            ok("抖动只跑一轮", anim["cnt"] == "1", f"{anim['cnt']}")
            ok("抖动时长收敛在 .5s 内",
               float(anim["dur"].rstrip("s")) <= .5, f"{anim['dur']}")
            ok("抖动期间卡片不跳位",
               abs(anim["y"] - anim["before"]) <= 4,
               f"before={anim['before']} during={anim['y']}（关键帧要带 translateY）")
            css_src = (ROOT / "webui.py").read_text(encoding="utf-8")
            login_css = re.search(r"LOGIN_HTML = r'''(.*?)'''", css_src, re.S).group(1)
            ok("关键帧保留 translateY(var(--cardY))",
               login_css.count("translateY(var(--cardY))") >= 5,
               "每个关键帧都要写，否则动画期间卡片瞬移")
            ok("抖动幅度不超过 2px",
               not re.search(r"translateX\((-?[3-9]|-?\d{2,})px\)", login_css),
               "原来是 ±4px")
            ok("有 prefers-reduced-motion 兜底",
               "prefers-reduced-motion" in login_css)
            pg.close()

            # ---- 6) 主界面确实会写缓存 ----
            pg2 = ctx.new_page()
            errs2: "list[str]" = []
            pg2.on("pageerror", lambda e: errs2.append(str(e)))
            pg2.goto(f"{base}/?token={token}", wait_until="load")
            try:
                pg2.wait_for_selector("#content .card", timeout=15000)
            except Exception:
                ok("主界面渲染出卡片", False, f"js错误={errs2[:2]}")
                browser.close()
                return 1
            pg2.wait_for_timeout(1200)
            raw = pg2.evaluate("()=>localStorage.getItem('xcbotLoginLook')||''")
            ok("主界面写入了外观缓存", bool(raw), raw[:80])
            try:
                look = json.loads(raw or "{}")
            except Exception:
                look = {}
            ok("缓存里有 preset", bool(look.get("preset")), f"{look.get('preset')!r}")
            ok("缓存体积远小于 localStorage 配额",
               len(raw) < 3_500_000, f"{len(raw)} 字节")
            ok("缓存不含 Token",
               token not in raw, "背景图 URL 带 token，转 data URL 后不该残留")
            ok("主界面无 JS 错误", not errs2, f"{errs2[:3]}")
            ok("shrinkBackgroundForLogin 已定义",
               pg2.evaluate("typeof shrinkBackgroundForLogin==='function'"))
            ctx.close()
            browser.close()
    finally:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
        webui.get_webui_config = orig

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

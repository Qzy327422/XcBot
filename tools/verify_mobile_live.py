# -*- coding: utf-8 -*-
"""真实 WebUI 的移动端布局验证（起真实服务 + 真实 app.js）。

与 verify_mobile_layout.py 的分工：那个用 SHIM 替掉 app.js，只验 CSS；
这个起真实 HTTP 服务、加载真实 app.js，验的是运行时才成立的东西——
applyWebuiAppearance 加的 has-bg / liquid-glass / liquid-glass-fallback，
renderNav 注入的导航，page-anim 动画，以及**桌面加载后缩到窄屏**这条路径
（用户实际的触发方式是「调整浏览器比例到移动端比例」，不是一开始就窄屏）。

只监听 127.0.0.1 的临时端口，跑完关掉。不写任何配置。

用法：python tools/verify_mobile_live.py
"""
from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

results: list[tuple[str, str, str]] = []


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


VIEW_JS = """()=>{const m=document.querySelector('.main'),a=document.querySelector('.app');
  if(!m||!a)return {err:'no .main/.app'};
  const mr=m.getBoundingClientRect(),ar=a.getBoundingClientRect();
  const s=getComputedStyle(document.querySelector('.sidebar'));
  return {mainY:Math.round(mr.y),mainH:Math.round(mr.height),mainW:Math.round(mr.width),
          appH:Math.round(ar.height),pos:s.position,
          rows:getComputedStyle(a).gridTemplateRows,
          cls:document.documentElement.className,
          // 首屏中心点落在谁身上——只剩背景图时这里会是 HTML/BODY 而不是页面元素
          hit:(()=>{const e=document.elementFromPoint(innerWidth/2,innerHeight/2);
                    return e?e.tagName+'.'+(e.className||'').toString().slice(0,40):'null'})(),
          // 正文里可见的卡片数
          cards:[...document.querySelectorAll('#content .card')].filter(c=>{
                  const r=c.getBoundingClientRect();
                  return r.height>0&&r.bottom>0&&r.top<innerHeight}).length}}"""

WIDTHS = [1440, 1100, 980, 900, 800, 700, 650, 560, 500, 430, 390, 360]


def main() -> int:
    import webui
    from playwright.sync_api import sync_playwright

    cfg = webui.get_webui_config()
    token = str(cfg.get("access_token") or "")
    _orig_get_cfg = webui.get_webui_config
    if not token:
        # 未设置 Token 时首页会弹「设置访问 Token」引导层，它盖住汉堡按钮，
        # 点击一直超时。注入一个临时 Token 让页面进正常态；不改 config.json。
        token = "verify-mobile-token"

        def _patched_get_cfg():
            c = dict(_orig_get_cfg())
            c["access_token"] = token
            c["enabled"] = True
            return c

        webui.get_webui_config = _patched_get_cfg
    port = free_port()
    srv = webui.start_webui(host="127.0.0.1", port=port)
    if srv is None:
        print("WebUI 未启动（可能 WebUI.enabled=false 或已有实例）")
        webui.get_webui_config = _orig_get_cfg
        return 1
    time.sleep(0.6)
    base = f"http://127.0.0.1:{port}/"
    url = base + (f"?token={token}" if token else "")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            # 用户的路径：桌面宽度打开，再缩窄
            pg = browser.new_page(viewport={"width": 1440, "height": 900})
            errs: list[str] = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(url, wait_until="load")
            try:
                pg.wait_for_selector("#content .card", timeout=15000)
            except Exception:
                ok("桌面首屏渲染出卡片", False, f"未等到 #content .card；js错误={errs[:2]}")
                browser.close()
                return 1
            pg.wait_for_timeout(1200)
            ok("无未捕获 JS 错误", not errs, f"{errs[:3]}")

            base_cls = pg.evaluate("document.documentElement.className")
            ok("真实外观已应用", True, f"html.class={base_cls!r}")

            for w in WIDTHS:
                pg.set_viewport_size({"width": w, "height": 844})
                pg.wait_for_timeout(450)
                v = pg.evaluate(VIEW_JS)
                narrow = w <= 980
                tag = f"缩到 {w}px"
                ok(f"{tag} 正文在首屏内", v["mainY"] <= 24, f"mainY={v['mainY']}")
                ok(f"{tag} 首屏中心可见页面元素",
                   v["hit"] not in ("HTML.", "BODY.", "null") and "app" not in v["hit"].split(".")[-1][:3],
                   f"hit={v['hit']}")
                ok(f"{tag} 首屏有可见卡片", v["cards"] >= 1, f"cards={v['cards']}")
                ok(f"{tag} 正文占满宽度" if narrow else f"{tag} 正文与侧栏分列",
                   (v["mainW"] >= w - 40) if narrow else (v["mainW"] < w - 180),
                   f"mainW={v['mainW']} vw={w}")
                if narrow:
                    ok(f"{tag} 抽屉已脱离文档流", v["pos"] == "fixed",
                       f"position={v['pos']} cls={v['cls']!r}")
                    # 真实内容比视口高是正常的（整页滚动）；出问题时的特征不是「高」，
                    # 而是栅格被撑成两行——侧栏还占着第一行，正文被挤到第二行。
                    ok(f"{tag} 栅格只有一行", len(v["rows"].split()) == 1,
                       f"rows={v['rows']}")
                    ok(f"{tag} 汉堡可见",
                       pg.locator("#navToggle").is_visible())
                else:
                    ok(f"{tag} 汉堡隐藏", not pg.locator("#navToggle").is_visible())

            # 窄屏下开合抽屉，并确认导航项真的能跳页
            pg.set_viewport_size({"width": 390, "height": 844})
            pg.wait_for_timeout(400)
            pg.locator("#navToggle").click()
            pg.wait_for_timeout(450)
            ok("抽屉打开后进屏",
               abs(pg.evaluate("document.querySelector('.sidebar').getBoundingClientRect().x")) <= 1)
            bg = pg.evaluate("getComputedStyle(document.querySelector('.sidebar')).backgroundColor")
            import re as _re
            mm = _re.match(r"rgba?\(([^)]*)\)", bg or "")
            a = 1.0
            if mm:
                parts = [p.strip() for p in mm.group(1).split(",")]
                if len(parts) == 4:
                    a = float(parts[3])
            ok("抽屉底色不透明", a >= .99, f"background={bg}")
            items = pg.locator(".custom-nav-item")
            n = items.count()
            ok("抽屉里有导航项", n >= 3, f"count={n}")
            if n >= 2:
                items.nth(1).click()
                pg.wait_for_timeout(700)
                ok("点导航项收起抽屉",
                   not pg.evaluate("document.body.classList.contains('nav-open')"))
                v = pg.evaluate(VIEW_JS)
                ok("跳页后正文仍在首屏", v["mainY"] <= 24 and v["cards"] >= 1,
                   f"mainY={v['mainY']} cards={v['cards']}")

            # 回到桌面，确认没留下抽屉态
            pg.set_viewport_size({"width": 1440, "height": 900})
            pg.wait_for_timeout(500)
            v = pg.evaluate(VIEW_JS)
            ok("回桌面侧栏归位", v["pos"] != "fixed", f"position={v['pos']}")
            ok("回桌面正文分列", v["mainW"] < 1440 - 180, f"mainW={v['mainW']}")
            ok("回桌面清除 nav-open",
               not pg.evaluate("document.body.classList.contains('nav-open')"))
            ok("全程无 JS 错误", not errs, f"{errs[:3]}")
            browser.close()
    finally:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
        webui.get_webui_config = _orig_get_cfg

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

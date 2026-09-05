# -*- coding: utf-8 -*-
"""移动端抽屉导航的布局验证（Playwright，本机离线）。

不启动真实 WebUI、不需要 token：从 webui.py 里取出 INDEX_HTML 的 <body> 骨架，
去掉 <script src="/static/app.js">（那份脚本一上来就打 /api 会全线报错），
换成本文件里同名的最小实现，只验 CSS 布局与开合行为。

**必须跑矩阵**：液态玻璃（html.liquid-glass）是用户的默认外观，
而玻璃块用 `html.liquid-glass .sidebar{position:relative !important}`（具体度 0,2,1）
压过裸 `.sidebar`（0,1,0）——曾经因为只在非玻璃模式下测，
把「窄屏整个 UI 消失、只剩背景图」的回归放了过去。
所以每条断言都在 glass=False / glass=True 两种模式下各跑一遍，
并在多个宽度上确认 .main 真的落在首屏内。

检查点：
  1) 窄屏（390x844）下抽屉初始在屏幕外，汉堡可见；
  2) 打开后抽屉进屏、遮罩可点、body 锁滚动；
  3) 抽屉是实底而不是透视（液态玻璃的半透明底色要被盖回去）；
  4) 页面无横向溢出；工具条里「保存设置」可达；
  5) **正文首屏可见**：多个窄宽度下 .main 的 y 在首屏内、.app 高度≈视口高；
  6) 桌面宽度（1440）下抽屉不生效、汉堡隐藏。

用法：python tools/verify_mobile_layout.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

results: list[tuple[str, str, str]] = []
_prefix = ""


def ok(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    full = f"{_prefix}{name}"
    results.append((status, full, detail))
    print(f"[{status}] {full}" + (f" - {detail}" if detail else ""))


SHIM = """
<script>
function el(i){return document.getElementById(i)}
function isMobileNav(){return window.matchMedia('(max-width:980px)').matches}
function setMobileNav(open){const b=document.body;b.classList.toggle('nav-open',!!open);const t=el('navToggle');if(t)t.setAttribute('aria-expanded',open?'true':'false')}
function toggleMobileNav(){setMobileNav(!document.body.classList.contains('nav-open'))}
function closeMobileNav(){setMobileNav(false)}
function gotoPage(){closeMobileNav()}
function toggleTheme(){}function saveAll(){}
window.addEventListener('resize',()=>{if(!isMobileNav()&&document.body.classList.contains('nav-open'))closeMobileNav()},{passive:true});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&document.body.classList.contains('nav-open'))closeMobileNav()});
// 导航项由真实 app.js 注入，这里塞几条等价结构，验抽屉里的点击会收起
el('nav').innerHTML='<div class="custom-nav">'+['\u6b22\u8fce','\u6a21\u578b','\u65e5\u5fd7'].map(t=>
  '<div class="custom-nav-item" onclick="gotoPage()"><div class="custom-nav-icon">\u2022</div><div class="custom-nav-label">'+t+'</div></div>').join('')+'</div>';
</script>
"""


def build_page() -> str:
    src = (ROOT / "webui.py").read_text(encoding="utf-8")
    m = re.search(r"INDEX_HTML = r'''(.*?)'''", src, re.S)
    if not m:
        raise SystemExit("没找到 INDEX_HTML")
    html = m.group(1)
    html = html.replace('<script src="/static/app.js"></script>', SHIM)
    # 图标资源来自 /assets，本地打开取不到，去掉避免控制台噪音
    html = html.replace('<img src="/assets/icon.jpg" alt="XcBot">', "")
    html = html.replace('<link rel="icon" href="/assets/icon.jpg">', "")
    css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
    html = html.replace(
        '<link rel="stylesheet" href="/static/app.css">', f"<style>{css}</style>"
    )
    return html


def alpha_of(bg: str) -> float:
    mm = re.match(r"rgba?\(([^)]*)\)", bg or "")
    if not mm:
        return 1.0
    parts = [p.strip() for p in mm.group(1).split(",")]
    return float(parts[3]) if len(parts) == 4 else 1.0


BOX_JS = "()=>{const r=document.querySelector('.sidebar').getBoundingClientRect();return {x:r.x,w:r.width}}"
# 这条是回归的照妖镜：侧栏没真的 fixed 时它会占满栅格第一行，
# .main 被顶到第二行（y≈854），.app 高度膨胀到 ~1455px，页面只剩背景图。
VIEW_JS = """()=>{const m=document.querySelector('.main').getBoundingClientRect();
  const a=document.querySelector('.app').getBoundingClientRect();
  const s=getComputedStyle(document.querySelector('.sidebar'));
  return {mainY:Math.round(m.y),mainH:Math.round(m.height),appH:Math.round(a.height),
          pos:s.position,rows:getComputedStyle(document.querySelector('.app')).gridTemplateRows}}"""

NARROW_WIDTHS = [980, 900, 700, 650, 500, 390]


def run(pg, page_html: str, glass: bool) -> None:
    global _prefix
    _prefix = ("[glass] " if glass else "[plain] ")
    pg.set_viewport_size({"width": 390, "height": 844})
    pg.set_content(page_html, wait_until="load")
    if glass:
        pg.evaluate("document.documentElement.classList.add('liquid-glass')")
        pg.wait_for_timeout(120)

    toggle = pg.locator("#navToggle")
    ok("窄屏汉堡可见", toggle.is_visible())

    vw = pg.evaluate("innerWidth")
    box = pg.evaluate(BOX_JS)
    ok("抽屉初始在屏幕外", box["x"] + box["w"] <= 1, f"right={box['x'] + box['w']:.1f}")
    ok("抽屉宽度不超过 82vw", box["w"] <= vw * 0.83, f"w={box['w']:.1f} vw={vw}")
    overflow = pg.evaluate("document.documentElement.scrollWidth - innerWidth")
    ok("收起时无横向溢出", overflow <= 1, f"溢出 {overflow}px")

    # ★ 正文首屏可见：逐个窄宽度扫，抽屉必须真的 fixed，.app 不能被撑成两行
    for w in NARROW_WIDTHS:
        pg.set_viewport_size({"width": w, "height": 844})
        pg.wait_for_timeout(120)
        v = pg.evaluate(VIEW_JS)
        ok(f"{w}px 抽屉已脱离文档流", v["pos"] == "fixed", f"position={v['pos']}")
        ok(f"{w}px 正文在首屏内", v["mainY"] <= 20, f"mainY={v['mainY']}")
        ok(f"{w}px 页面未被侧栏撑高", v["appH"] <= 844 + 40,
           f"appH={v['appH']} rows={v['rows']}")
        ok(f"{w}px 正文有实际高度", v["mainH"] >= 700, f"mainH={v['mainH']}")
    pg.set_viewport_size({"width": 390, "height": 844})
    pg.wait_for_timeout(150)

    toggle.click()
    pg.wait_for_timeout(400)
    box2 = pg.evaluate(BOX_JS)
    ok("打开后抽屉进屏", abs(box2["x"]) <= 1, f"x={box2['x']:.1f}")
    ok("打开后仍无横向溢出",
       pg.evaluate("document.documentElement.scrollWidth - innerWidth") <= 1)
    ok("aria-expanded 同步", toggle.get_attribute("aria-expanded") == "true")

    scrim = pg.evaluate("()=>{const s=getComputedStyle(document.getElementById('navScrim'));return {op:s.opacity,pe:s.pointerEvents,z:s.zIndex}}")
    ok("遮罩可见且可点", float(scrim["op"]) > .5 and scrim["pe"] != "none", f"{scrim}")
    sz = pg.evaluate("getComputedStyle(document.querySelector('.sidebar')).zIndex")
    ok("抽屉层级高于遮罩", int(sz) > int(scrim["z"]), f"sidebar={sz} scrim={scrim['z']}")
    ok("打开时锁 body 滚动",
       pg.evaluate("getComputedStyle(document.body).overflow") == "hidden")

    # 抽屉是浮层，底色必须不透明；否则会透出正文
    for theme in ("dark", "light"):
        pg.evaluate(f"document.documentElement.setAttribute('data-theme','{theme}')")
        pg.wait_for_timeout(80)
        bg = pg.evaluate("getComputedStyle(document.querySelector('.sidebar')).backgroundColor")
        ok(f"{theme} 抽屉底色不透明", alpha_of(bg) >= .99, f"background={bg}")
    pg.evaluate("document.documentElement.setAttribute('data-theme','dark')")

    # 抽屉里点导航项应当收起
    pg.locator(".custom-nav-item").first.click()
    pg.wait_for_timeout(350)
    ok("点导航项后抽屉收起",
       not pg.evaluate("document.body.classList.contains('nav-open')"))

    toggle.click()
    pg.wait_for_timeout(300)
    pg.keyboard.press("Escape")
    pg.wait_for_timeout(300)
    ok("Esc 收起抽屉", not pg.evaluate("document.body.classList.contains('nav-open')"))

    toggle.click()
    pg.wait_for_timeout(300)
    pg.evaluate("document.getElementById('navScrim').click()")
    pg.wait_for_timeout(300)
    ok("点遮罩收起抽屉", not pg.evaluate("document.body.classList.contains('nav-open')"))

    # 汉堡是窄屏唯一导航入口，正文滚动时不能滑走
    pg.evaluate("()=>{const m=document.querySelector('.main');window.scrollTo(0,400);m.scrollTop=400}")
    pg.wait_for_timeout(150)
    tb = pg.evaluate("()=>{const r=document.querySelector('.topbar').getBoundingClientRect();return Math.round(r.y)}")
    ok("滚动后 topbar 仍在视口内", tb <= 60, f"topbarY={tb}")
    pg.evaluate("window.scrollTo(0,0)")

    # 工具条：「保存设置」必须能滑到（横向可滚），且按钮不被压成两行。
    # 用垂直中心比对而不是 top —— saveState 是 32px 的 pill，按钮是 42px，
    # align-items:center 下同一行的 top 本来就不同。
    tbar = pg.evaluate("""()=>{const t=document.querySelector('.toolbar');
      const btn=[...t.querySelectorAll('button')].pop();
      const mids=[...t.children].map(c=>{const r=c.getBoundingClientRect();return Math.round(r.top+r.height/2)});
      return {rows:new Set(mids).size, btn:btn.textContent.trim()}}""")
    ok("工具条保持单行", tbar["rows"] == 1, f"rows={tbar['rows']}")
    ok("最后一个按钮是保存设置", "保存" in tbar["btn"], f"btn={tbar['btn']}")
    pg.evaluate("()=>{const t=document.querySelector('.toolbar');t.scrollLeft=t.scrollWidth}")
    vis = pg.evaluate("""()=>{const t=document.querySelector('.toolbar');
      const b=[...t.querySelectorAll('button')].pop().getBoundingClientRect();
      const r=t.getBoundingClientRect();return b.right<=r.right+1&&b.left>=r.left-1}""")
    ok("保存设置可滑入视口", vis)

    h2 = pg.evaluate("()=>getComputedStyle(document.getElementById('pageTitle')).fontSize")
    ok("标题字号已收窄", float(h2.rstrip("px")) <= 20, f"font-size={h2}")

    # 桌面宽度：抽屉规则不生效
    pg.set_viewport_size({"width": 1440, "height": 900})
    pg.wait_for_timeout(300)
    ok("桌面隐藏汉堡", not toggle.is_visible())
    desk = pg.evaluate("()=>{const s=getComputedStyle(document.querySelector('.sidebar'));return {pos:s.position,tf:s.transform,w:document.querySelector('.sidebar').getBoundingClientRect().width}}")
    # 桌面本来就是 position:relative（app.css 的玻璃面板块，无媒体查询），
    # 这里只要求不是抽屉态的 fixed。
    ok("桌面侧栏不是 fixed", desk["pos"] != "fixed", f"position={desk['pos']}")
    ok("桌面侧栏无位移", desk["tf"] in ("none", "matrix(1, 0, 0, 1, 0, 0)"), f"transform={desk['tf']}")
    ok("桌面侧栏回到栅格列宽", desk["w"] > 200, f"width={desk['w']:.1f}")
    ok("桌面无横向溢出",
       pg.evaluate("document.documentElement.scrollWidth - innerWidth") <= 1)
    ok("桌面正文与侧栏并排",
       pg.evaluate("()=>{const s=document.querySelector('.sidebar').getBoundingClientRect();const m=document.querySelector('.main').getBoundingClientRect();return m.x>=s.right-1}"))

    # 放宽窗口时残留的 nav-open 必须被清掉
    pg.set_viewport_size({"width": 390, "height": 844})
    pg.wait_for_timeout(200)
    toggle.click()
    pg.wait_for_timeout(300)
    pg.set_viewport_size({"width": 1440, "height": 900})
    pg.wait_for_timeout(300)
    ok("放宽窗口自动清除 nav-open",
       not pg.evaluate("document.body.classList.contains('nav-open')"))


def main() -> int:
    from playwright.sync_api import sync_playwright

    page_html = build_page()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        pg = browser.new_page(viewport={"width": 390, "height": 844})
        for glass in (False, True):
            run(pg, page_html, glass)
        browser.close()

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

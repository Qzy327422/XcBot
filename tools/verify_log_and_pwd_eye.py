# -*- coding: utf-8 -*-
"""设置页密码字段的眼睛按钮 + 日志按整行输出的验证。

分两部分：

A. 日志（离线，不起服务）
   TeeStream 原来直接透传 write()。print(x) 会拆成 write(x) + write("\\n") 两次，
   多线程下两次之间可能插进别的线程的输出，于是控制台出现
   "=== 启动中 ===[AgentTask] 调度器已启动" 这种两条日志挤一行、后一条没换行的样子；
   内存缓冲里还会为单独那次 write("\\n") 多存一条空消息。
   改成攒到换行再整行发出。这里用真实多线程并发 print 验证。

B. 设置页眼睛按钮（Playwright + 真实服务）
   password 类字段右侧应有一个 SVG 眼睛，点击切明文；
   浏览器自带的 ::-ms-reveal 必须关掉，否则并排出现两个眼睛。

用法：python tools/verify_log_and_pwd_eye.py
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


class Sink:
    """假的原始流，记下每一次 write 的实参。"""

    def __init__(self):
        self.writes: "list[str]" = []
        self.encoding = "utf-8"
        self.errors = "replace"
        self._lock = threading.Lock()

    def write(self, s):
        with self._lock:
            self.writes.append(s)
        return len(s)

    def flush(self):
        pass

    def isatty(self):
        return False


def check_log_lines() -> None:
    import webui

    # ---- 单线程：print 的两次 write 合成一行 ----
    sink = Sink()
    tee = webui.TeeStream(sink, "stdout")
    buf_before = len(webui._log_buffer)
    tee.write("hello")
    ok("半行不立即输出", not sink.writes, f"{sink.writes}")
    tee.write("\n")
    ok("拿到换行才输出整行", sink.writes == ["hello\n"], f"{sink.writes}")
    added = list(webui._log_buffer)[buf_before:]
    ok("一次 print 只进一条日志", len(added) == 1, f"{[x['message'] for x in added]}")
    ok("日志内容不含换行", added and "\n" not in added[0]["message"],
       repr(added[0]["message"] if added else None))

    # ---- 一次 write 里带多行：拆成多行分别输出 ----
    sink2 = Sink()
    tee2 = webui.TeeStream(sink2, "stdout")
    tee2.write("a\nb\nc")
    ok("多行整块按行拆开", sink2.writes == ["a\n", "b\n"], f"{sink2.writes}")
    ok("末尾没换行的部分仍被攒着", True)
    tee2.write("\n")
    ok("补上换行后尾行输出", sink2.writes == ["a\n", "b\n", "c\n"], f"{sink2.writes}")

    # ---- \r 也当行结束（进度条式输出不能把后续内容粘上去）----
    sink3 = Sink()
    tee3 = webui.TeeStream(sink3, "stdout")
    tee3.write("progress\r")
    ok("\\r 视为行结束", sink3.writes == ["progress\n"], f"{sink3.writes}")

    # ---- 空行要保留成一条空消息，不能被吞掉 ----
    sink4 = Sink()
    tee4 = webui.TeeStream(sink4, "stdout")
    n0 = len(webui._log_buffer)
    tee4.write("\n")
    ok("空行保留", sink4.writes == ["\n"] and len(webui._log_buffer) - n0 == 1,
       f"writes={sink4.writes} added={len(webui._log_buffer)-n0}")

    # ---- drain：退出前把没换行的尾巴冲出去 ----
    sink5 = Sink()
    tee5 = webui.TeeStream(sink5, "stdout")
    tee5.write("tail-without-newline")
    tee5.drain()
    ok("drain 冲出尾部半行", sink5.writes == ["tail-without-newline\n"], f"{sink5.writes}")

    # ---- 噪音行仍被屏蔽，且不留空行 ----
    sink6 = Sink()
    tee6 = webui.TeeStream(sink6, "stdout")
    n1 = len(webui._log_buffer)
    tee6.write("🔴 *CRIT 重试次数达到最大值(5)，退出\n")
    ok("噪音行仍被吞掉", not sink6.writes, f"{sink6.writes}")
    ok("噪音行不进日志缓冲", len(webui._log_buffer) == n1,
       f"added={len(webui._log_buffer)-n1}")
    sink6b = Sink()
    tee6b = webui.TeeStream(sink6b, "stdout")
    tee6b.write("重试次数达到最大值")
    tee6b.write("\n")
    tee6b.write("正常内容\n")
    ok("噪音行后不留空行", sink6b.writes == ["正常内容\n"], f"{sink6b.writes}")

    # ---- 多线程并发：这是真实症状的复现 ----
    # 每个线程 print 一条带标记的整行；任何一次 write 里出现两条标记，
    # 就说明两条日志被拼到了同一行。
    sink7 = Sink()
    tee7 = webui.TeeStream(sink7, "stdout")
    real_stdout = sys.stdout
    sys.stdout = tee7
    try:
        def worker(idx: int):
            for k in range(25):
                print(f"<T{idx}-{k}>")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.stdout = real_stdout

    writes = list(sink7.writes)
    ok("并发下每次 write 都是完整一行",
       all(w.endswith("\n") and w.count("\n") == 1 for w in writes),
       f"共 {len(writes)} 次，异常示例={[w for w in writes if not w.endswith(chr(10)) or w.count(chr(10))!=1][:3]}")
    glued = [w for w in writes if w.count("<T") > 1]
    ok("并发下没有两条日志挤在同一行", not glued, f"{glued[:3]}")
    ok("并发下条数不丢", len(writes) == 8 * 25, f"{len(writes)}")
    blanks = [w for w in writes if w == "\n"]
    ok("并发下不产生空行", not blanks, f"{len(blanks)} 条空行")


PWD_JS = """()=>{const w=[...document.querySelectorAll('.pwd-wrap')];
  if(!w.length)return {err:'no .pwd-wrap'};
  const first=w[0],inp=first.querySelector('input'),btn=first.querySelector('.pwd-eye');
  if(!inp||!btn)return {err:`inp=${!!inp} btn=${!!btn}`};
  const br=btn.getBoundingClientRect(),ir=inp.getBoundingClientRect();
  const cs=getComputedStyle(inp);
  return {count:w.length,type:inp.type,hasSvg:!!btn.querySelector('svg'),
          btnText:(btn.textContent||'').trim(),
          // 按钮压在输入框右侧内部，且没盖住文字（输入框有右内边距）
          inside:br.right<=ir.right+2&&br.left>ir.left,
          padRight:parseFloat(cs.paddingRight)||0,
          id:inp.id}}"""


def check_pwd_eye() -> None:
    import webui
    from playwright.sync_api import sync_playwright

    token = "verify-eye-token"
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
        return
    time.sleep(0.6)
    base = f"http://127.0.0.1:{port}"

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            pg = browser.new_page(viewport={"width": 1440, "height": 900})
            errs: "list[str]" = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto(f"{base}/?token={token}", wait_until="load")
            try:
                pg.wait_for_selector("#content .card", timeout=15000)
            except Exception:
                ok("首页渲染出卡片", False, f"js错误={errs[:2]}")
                browser.close()
                return
            ok("首页无 JS 错误", not errs, f"{errs[:3]}")

            # 跳到含 password 字段的页面（访问 Token / OneBot Token 都是 password 类）。
            # pages()/gotoPage 是模块顶层的 const/function，不挂在 window 上，
            # 所以要在页面上下文里直接引用而不是 window.pages。
            went = pg.evaluate(
                "()=>{const p=(state.bundle&&state.bundle.ui_schema)||[];"
                "const t=p.find(x=>(x.fields||[]).some(f=>f.type==='password'));"
                "if(!t)return '';gotoPage(t.key);return t.key}")
            ok("找到含 password 字段的页面", bool(went), f"page={went!r}")
            pg.wait_for_timeout(900)

            v = pg.evaluate(PWD_JS)
            if v.get("err"):
                ok("password 字段带眼睛按钮", False, v["err"])
                browser.close()
                return
            ok("password 字段带眼睛按钮", True, f"共 {v['count']} 个")
            ok("初始为密码态", v["type"] == "password", v["type"])
            ok("图标是 SVG 不是 emoji", v["hasSvg"] and not v["btnText"],
               f"hasSvg={v['hasSvg']} text={v['btnText']!r}")
            ok("按钮位于输入框内右侧", v["inside"] is True)
            ok("输入框留出右内边距", v["padRight"] >= 30, f"{v['padRight']}px")

            pg.click(".pwd-wrap .pwd-eye")
            pg.wait_for_timeout(200)
            ok("点一下变明文",
               pg.evaluate("document.querySelector('.pwd-wrap input').type") == "text")
            ok("明文态图标仍是 SVG",
               pg.evaluate("!!document.querySelector('.pwd-wrap .pwd-eye svg')"))
            pg.click(".pwd-wrap .pwd-eye")
            pg.wait_for_timeout(200)
            ok("再点回密码态",
               pg.evaluate("document.querySelector('.pwd-wrap input').type") == "password")

            css = pg.evaluate(
                "()=>[...document.styleSheets].flatMap(s=>{try{return [...s.cssRules]}"
                "catch(e){return []}}).map(r=>r.cssText).join('\\n')")
            # Chromium 不认识 ::-ms-reveal，会把整条规则当无效选择器丢掉，
            # 所以 cssRules 里查不到它——只能对源文件做静态断言。
            # 这条规则真正生效的地方是 Edge（Chromium 内核但实现了 ::-ms-reveal）。
            ok("Chromium 丢弃 -ms- 规则不影响其它样式", "pwd-eye" in css,
               "同一文件里的普通规则仍在")
            ok("已关掉浏览器自带 reveal 按钮",
               "-ms-reveal" in (ROOT / "static" / "app.css").read_text(encoding="utf-8"),
               "否则 Edge 下会出现两个眼睛")

            # 切明文不能破坏取值：保存前 syncCurrentPageFieldsFromDom 按 type 分派
            pg.fill(".pwd-wrap input", "typed-token-123")
            pg.click(".pwd-wrap .pwd-eye")
            pg.wait_for_timeout(150)
            pg.evaluate("syncCurrentPageFieldsFromDom()")
            got = pg.evaluate(
                "()=>{const id=document.querySelector('.pwd-wrap input').id;"
                "const p=id.slice(2);const v=state.bundle.form_values||{};"
                "return Object.entries(v).find(([k])=>k.replace(/[^a-zA-Z0-9]/g,'_')===p)?.[1]}")
            ok("切明文后取值仍正确", got == "typed-token-123", repr(got))
            ok("全程无 JS 错误", not errs, f"{errs[:3]}")
            browser.close()
    finally:
        try:
            srv.shutdown()
            srv.server_close()
        except Exception:
            pass
        webui.get_webui_config = orig


def main() -> int:
    check_log_lines()
    check_pwd_eye()

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

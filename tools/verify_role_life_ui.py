"""离线检查「角色生活」页面外观与交互；全部接口由内存夹具响应。

不 import main/webui 的启动逻辑，不读真实 config/聊天，不启动 QQ、不联网、不调用模型。
真实来源只有：webui.py 里的 INDEX_HTML 字符串、static/app.js、static/app.css。

用法：
    python tools/verify_role_life_ui.py --phase before
    python tools/verify_role_life_ui.py --phase after --output-dir B:/tmp/rl-after
    python tools/verify_role_life_ui.py --phase after --assert-clean

--phase 只影响报告标签；--assert-clean 时把“必须干净”的发现（横向溢出、文字
重复转义、控件越界/重叠）当作失败。默认只产出报告和截图，便于改前留基线。
"""
from __future__ import annotations

import argparse
import ast
import copy
import json
import re
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
LONG_CN = "整理书架时发现一封很久以前的信，坐在窗边读了两遍，窗外的光从左边慢慢移到右边，" * 3
LONG_EN = "VeryLongModelNameWithoutAnySpaces" * 8

# ---------------------------------------------------------------- 夹具

DEFAULT_CONFIG = {
    "enabled": True, "plan_enabled": True, "activity_enabled": True,
    "sleep_enabled": True, "diary_enabled": True, "dream_enabled": True,
    "maintenance_enabled": False, "timezone": "Asia/Shanghai",
    "sleep_start": "00:30", "sleep_hours": 8, "diary_time": "23:50",
    "activities_per_day": 5, "role_daily_limit": 24, "retention_days": 90,
    "maintenance_interval_seconds": 1800, "model_override": "", "preference": "",
}

PLAN_ITEMS = [
    {"id": "x-p1-1", "title": "晨间阅读", "purpose": "把昨晚没读完的一章看完",
     "start": 1760000000, "end": 1760003600, "status": "completed", "experience_id": "exp-1"},
    {"id": "x-p1-2", "title": "整理书桌", "purpose": "把散落的纸和笔收好",
     "start": 1760003600, "end": 1760007200, "status": "active", "experience_id": ""},
    {"id": "x-p1-3", "title": LONG_CN.strip(), "purpose": "很长的中文活动名称用于检查换行",
     "start": 1760007200, "end": 1760010800, "status": "planned", "experience_id": ""},
    {"id": "x-p1-4", "title": "散步", "purpose": "绕小区一圈",
     "start": 1760010800, "end": 1760014400, "status": "interrupted", "experience_id": ""},
]

JOBS = [
    {"id": "j1", "kind": "plan", "status": "failed", "created_at": 1760000000,
     "finished_at": 1760000100, "error": "timeout: provider did not answer in 60s " + LONG_EN,
     "attempts": 1, "request_id": "r1", "retry_of": ""},
    {"id": "j2", "kind": "activity", "status": "unknown", "created_at": 1760000200,
     "finished_at": 0, "error": "", "attempts": 2, "request_id": "r2", "retry_of": ""},
    {"id": "j3", "kind": "diary", "status": "succeeded", "created_at": 1760000300,
     "finished_at": 1760000400, "error": "", "attempts": 1, "request_id": "r3", "retry_of": ""},
    {"id": "j4", "kind": "dream", "status": "pending", "created_at": 1760000500,
     "finished_at": 0, "error": "", "attempts": 0, "request_id": "r4", "retry_of": ""},
]


def base_state(**overrides):
    state = {
        "connected": True, "current_account": "10001",
        "known_accounts": ["10001", "20002"],
        "roles": {"10001": ["p1", "p2"], "20002": ["p9"]},
        "account": "10001", "role_id": "p1", "editable": True, "read_only_reason": "",
        "presets": [{"id": "p1", "name": "甲"}, {"id": "p2", "name": "乙"}],
        "models": [{"provider_id": "x", "model": "m", "label": "x/m"},
                   {"provider_id": "y", "model": LONG_EN, "label": "y/" + LONG_EN}],
        "default_config": copy.deepcopy(DEFAULT_CONFIG),
        "config": copy.deepcopy(DEFAULT_CONFIG), "config_revision": "abc",
        "status": {
            "enabled": True, "available": True, "corrupt": False,
            "account": "10001", "role_id": "p1",
            "revision": "rev-1", "epoch": 2, "state_version": 7, "last_advance": 0,
            "date": "2026-03-10", "sleeping": False, "sleep": None,
            "current_activity": PLAN_ITEMS[1],
            "plan": {"plan_id": "p", "serial": 1, "items": copy.deepcopy(PLAN_ITEMS)},
            "mood": {"mood": "平静", "energy": "尚可", "updated_at": 1760000000},
            "recent": [
                {"id": "exp-1", "kind": "activity", "text": "看完一章，记下了两句。",
                 "date": "2026-03-10", "dream": False},
                {"id": "diary-1", "kind": "diary", "text": LONG_CN,
                 "date": "2026-03-09", "dream": False},
                {"id": "dream-1", "kind": "dream", "text": "一条没有尽头的走廊。",
                 "date": "2026-03-09", "dream": True},
            ],
            "usage": {"role_24h": 3, "role_limit": 24, "account_limit": 192,
                      "account_id": "10001"},
            "queue": 0, "jobs": copy.deepcopy(JOBS),
            "metrics": {"last_error": "timeout: provider did not answer in 60s",
                        "last_error_at": 1760000100, "last_success_at": 1760000400},
            "next_node": {"at": 1760010800, "label": "写日记"},
        },
    }
    state.update(overrides)
    return state


def record_pages(kind):
    if kind in ("activity", "experiences"):
        items = [{"id": "exp-1", "kind": "activity", "text": "看完一章，记下了两句。",
                  "date": "2026-03-10", "dream": False, "created_at": 1760000000},
                 {"id": "exp-2", "kind": "activity", "text": LONG_CN,
                  "date": "2026-03-09", "dream": False, "created_at": 1759990000}]
    elif kind == "diary":
        items = [{"id": "diary-1", "kind": "diary", "text": LONG_CN,
                  "date": "2026-03-10", "dream": False, "created_at": 1760000000},
                 {"id": "diary-2", "kind": "diary",
                  "text": '<img src=x onerror=alert(1)> 这是一段会被转义的内容',
                  "date": "2026-03-09", "dream": False, "created_at": 1759990000}]
    elif kind == "dream":
        items = [{"id": "dream-1", "kind": "dream", "text": "一条没有尽头的走廊。",
                  "date": "2026-03-10", "dream": True, "created_at": 1760000000}]
    elif kind == "plans":
        items = [{"id": "x-p1-1", "date": "2026-03-10", "title": "晨间阅读",
                  "purpose": "看书", "status": "completed", "experience_id": "exp-1"},
                 {"id": "x-p1-3", "date": "2026-03-10", "title": LONG_CN.strip(),
                  "purpose": LONG_EN, "status": "planned", "experience_id": ""}]
    else:  # jobs
        items = copy.deepcopy(JOBS)
    return {"items": items, "page": 1, "page_size": 20,
            "total": 45, "pages": 3}


def state_fixtures():
    fixtures = {}

    fixtures["normal"] = base_state()

    fixtures["disconnected"] = base_state(
        connected=False, current_account="", known_accounts=[], roles={},
        account="", role_id="", editable=False,
        read_only_reason="未连接 QQ/运行时，页面只读",
        presets=[{"id": "p1", "name": "甲"}], config=None, config_revision="",
        status={"enabled": False, "available": False, "corrupt": False,
                "revision": "", "epoch": 0, "state_version": 0, "jobs": [],
                "metrics": {}, "sleeping": False, "plan": {"items": []},
                "recent": [], "usage": {}})

    disabled = base_state()
    disabled["status"] = dict(disabled["status"], enabled=False,
                              plan={"plan_id": "", "items": []},
                              current_activity=None, next_node=None, recent=[])
    fixtures["disabled"] = disabled

    empty = base_state()
    empty["status"] = dict(empty["status"], plan={"items": []},
                           current_activity=None, recent=[], jobs=[], metrics={},
                           next_node=None)
    fixtures["empty"] = empty

    corrupt = base_state()
    corrupt["status"] = {"enabled": True, "available": False, "corrupt": True,
                         "account": "10001", "role_id": "p1",
                         "error": "state identity mismatch: epoch changed",
                         "jobs": [], "metrics": {}, "usage": {}}
    fixtures["corrupt"] = corrupt

    unknown = base_state()
    unknown["status"] = dict(unknown["status"], budget_unknown=True,
                             budget_unknown_until=1893456000,
                             corrupt_backup="state.corrupt-1760000000000.json.bak",
                             recovered_from_corrupt=True)
    fixtures["budget_unknown"] = unknown

    long_state = base_state()
    long_state["presets"] = [{"id": "p1", "name": "很长的人格预设名称" * 6},
                             {"id": "p2", "name": "乙"}]
    long_state["status"]["plan"]["items"][0]["title"] = "活动标题" * 20
    long_state["status"]["plan"]["items"][0]["purpose"] = LONG_EN
    long_state["status"]["metrics"]["last_error"] = LONG_CN
    fixtures["long"] = long_state
    return fixtures


# ---------------------------------------------------------------- 资源载入

def load_sources():
    source = (ROOT / "webui.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef)
             and node.name in ("field", "build_ui_schema")]
    namespace = {"Dict": dict, "Any": object}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "schema", "exec"), namespace)
    schema = namespace["build_ui_schema"]({})
    values = {f["path"]: copy.deepcopy(f.get("default"))
              for page in schema for f in page["fields"]}
    feature_tree = ast.parse((ROOT / "webui_core/features.py").read_text(encoding="utf-8"))
    feature_meta = next(
        ast.literal_eval(node.value) for node in feature_tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "FEATURE_META" for t in node.targets))
    html = re.search(r"INDEX_HTML = r'''(.*?)'''", source, re.S).group(1)
    values.update({
        "ProactiveReply.group.enabled": False,
        "ProactiveReply.private.enabled": False,
        "Agent.enabled": False,
        "WebUI.liquid_glass": False,
        "Others.llm_providers": [{
            "id": "offline", "base_url": "", "keys": [],
            "models": [{"name": "offline/model", "enabled": True}],
            "planner_models": [], "embedding_models": []}],
        "Others.personality_presets": [{"id": "p1", "name": "甲", "prompt": "离线布局测试"},
                                       {"id": "p2", "name": "乙", "prompt": ""}],
        "Others.active_personality_preset": "p1",
    })
    bundle = {"ui_schema": schema, "form_values": values,
              "status": {"project": "XcBot",
                         "connection_status": {"state": "connected", "text": "已连接"},
                         "connection": {"host": "127.0.0.1", "port": 8080}},
              "feature_switches": {f["key"]: True for f in feature_meta},
              "feature_meta": feature_meta, "agent_tools": {}, "agent_tool_meta": [],
              "config_fingerprint": "offline"}
    js = (ROOT / "static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "static/app.css").read_text(encoding="utf-8")
    return {"html": html, "js": js, "css": css, "bundle": bundle}


# ---------------------------------------------------------------- 几何审计

GEOMETRY = r"""()=>{
  const out={overflow:document.documentElement.scrollWidth>innerWidth,
    docWidth:document.documentElement.scrollWidth,innerWidth,
    outOfBounds:[],clipped:[],overlap:[],smallControls:[],notes:[]};
  const main=document.querySelector('.main');
  if(!main)return out;
  const mainRect=main.getBoundingClientRect();
  const nodes=[...document.querySelectorAll('#content .card,#content .field,#content .stat,#content .setting-row')];
  for(const n of nodes){
    const r=n.getBoundingClientRect();if(!r.width||!r.height)continue;
    if(r.left<mainRect.left-1||r.right>mainRect.right+1)
      out.outOfBounds.push([n.className,Math.round(r.left),Math.round(r.right),
        Math.round(mainRect.left),Math.round(mainRect.right)]);
    for(let p=n.parentElement;p&&p!==document.body;p=p.parentElement){
      const s=getComputedStyle(p);
      if(!/(hidden|clip|auto|scroll)/.test(s.overflowX))continue;
      const b=p.getBoundingClientRect(),left=b.left+p.clientLeft;
      if(r.left<left-1||r.right>left+p.clientWidth+1)
        out.clipped.push([n.className,p.id||p.className,Math.round(r.left-left),Math.round(r.right-left-p.clientWidth)]);
      break;
    }
    if(n.matches('.field,.setting-row')){
      for(const other of n.parentElement.querySelectorAll(':scope > .field,:scope > .setting-row')){
        if(other===n)continue;const b=other.getBoundingClientRect();
        if(Math.min(r.right,b.right)-Math.max(r.left,b.left)>1&&Math.min(r.bottom,b.bottom)-Math.max(r.top,b.top)>1)
          out.overlap.push([n.className,other.className]);
      }
    }
  }
  const overflows=[];
  for(const p of document.querySelectorAll('#content,#content *')){
    const items=[...p.children].filter(n=>n.matches('.card,.field,.stat,.setting-row'))
      .map(n=>n.getBoundingClientRect()).filter(r=>r.width&&r.height);
    for(let i=0;i<items.length;i++)for(let j=i+1;j<items.length;j++){
      const a=items[i],b=items[j];
      if(Math.min(a.right,b.right)-Math.max(a.left,b.left)>1&&Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>1)
        overflows.push(['重叠',p.className]);
    }
  }
  out.overlap.push(...overflows);
  for(const n of document.querySelectorAll('#content button,#content input,#content select,#content textarea')){
    if(n.type==='hidden'||!n.getClientRects().length)continue;
    const s=getComputedStyle(n);if(s.visibility==='hidden'||s.display==='none')continue;
    const r=n.getBoundingClientRect(),w=Math.round(r.width),h=Math.round(r.height);
    // 文本标签页允许窄，但要够高；真正的表单控件要求两边都不小于 24。
    const textTab=n.matches('[role=tab]');
    if(h>0&&(h<24||(textTab?h<44:w<24)))
      out.smallControls.push([n.id||n.className||n.tagName,w,h]);
  }
  out.primaryButtons=[...document.querySelectorAll('#content .btn.primary')].filter(n=>n.getClientRects().length).length;
  out.switchNotButton=[...document.querySelectorAll('#content .switch')].filter(n=>!n.matches('button')).length;
  out.tabs=[...document.querySelectorAll('#content [role=tab]')].map(n=>({text:n.textContent.trim(),selected:n.getAttribute('aria-selected')}));
  out.switchesRole=[...document.querySelectorAll('#content [role=switch]')].length;
  // 重复转义：页面上出现源码形态的标签
  out.escapedTags=[...document.querySelectorAll('#content h3,#content .pill,#content .desc,#content div')]
    .filter(n=>n.children.length===0&&/(<span|&lt;span|&lt;\/span)/.test(n.textContent||''))
    .slice(0,5).map(n=>(n.textContent||'').slice(0,60));
  // 记录正文换行是否保留
  out.nowrapLong=[...document.querySelectorAll('#content .field>div,#content .desc')]
    .filter(n=>n.children.length===0&&/\n/.test(n.textContent||'')&&getComputedStyle(n).whiteSpace!=='pre-wrap')
    .slice(0,5).map(n=>(n.textContent||'').slice(0,40));
  out.recordPreWrap=[...document.querySelectorAll('#content .field>div')]
    .filter(n=>!/\n/.test(n.textContent||'')).length;
  // 玻璃层/浮层是否挡住可见控件：取每个控件中心点做命中测试。
  out.blocked=[];
  const visible=n=>{try{return n.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})}
    catch(e){return !!(n.offsetParent||n.getClientRects().length)}};
  for(const n of document.querySelectorAll('#content button,#content [role=tab],#content input,#content select')){
    if(!n.getClientRects().length||n.disabled)continue;
    if(!visible(n))continue;
    // 折叠 details 里的元素虽然仍有布局盒，但不可见，不算。
    if(n.closest('details:not([open])'))continue;
    const r=n.getBoundingClientRect();if(!r.width||!r.height)continue;
    const x=r.left+r.width/2,y=r.top+r.height/2;
    // 正好落在视口边缘的点 elementFromPoint 会返回 null，不算遮挡。
    if(y<1||y>innerHeight-1||x<1||x>innerWidth-1)continue;
    const hit=document.elementFromPoint(x,y);
    if(!hit||!(hit===n||n.contains(hit)||hit.contains(n)))
      out.blocked.push([n.id||n.className||n.tagName,hit?(hit.id||hit.className||hit.tagName):'null']);
  }
  // 卡片内容是否被 overflow:hidden 真裁切。
  out.clippedContent=[...document.querySelectorAll('#content .card,#content .life-record')]
    .filter(n=>{const s=getComputedStyle(n);
      return /hidden|clip/.test(s.overflowY||'')&&n.scrollHeight>n.clientHeight+2})
    .map(n=>[n.className,n.scrollHeight,n.clientHeight]);
  // 粘性保存条是否覆盖正文/危险区（只在它处于页面内可见时算）。
  // 粘性保存条只在“已经滚到底仍被盖住”时才算问题；滚到底能看到就不算。
  const bar=document.querySelector('.life-savebar');
  out.stickyCovered=[];out.stickyAtBottom=false;
  if(bar){
    const main=document.querySelector('.main');
    out.stickyAtBottom=!!main&&main.scrollTop>=main.scrollHeight-main.clientHeight-2;
    const b=bar.getBoundingClientRect();
    if(out.stickyAtBottom&&b.top<innerHeight&&b.bottom>0)
      out.stickyCovered=[...document.querySelectorAll(
        '.life-settings-main .life-caption,.life-settings-main .life-danger-zone')]
        .filter(n=>{const r=n.getBoundingClientRect();return r.bottom>b.top+1&&r.top<b.bottom-1})
        .map(n=>n.textContent.trim().slice(0,30));
  }
  return out;
}"""


def audit(page):
    return page.evaluate(GEOMETRY)


# ---------------------------------------------------------------- 交互套件

def interaction_checks(page, current, fixtures, src):
    """真实前端函数 + fake api/confirm/prompt；不访问真实后端。"""
    checks = []

    def record(name, ok, detail=""):
        checks.append({"name": name, "ok": bool(ok), "detail": "" if ok else str(detail)[:400]})

    def reset(fixture):
        current["state"] = fixture
        page.evaluate("""s=>{
          roleLifeState={data:s,selectorData:s,loading:false,error:'',
            account:s.account||'',role:s.role_id||'',tab:'overview',subTab:'activity',
            todayTab:'today',generation:0,sequence:0,updatedAt:0,saving:false,
            page:{activity:1,diary:1,dream:1,plans:1,jobs:1},
            records:{activity:null,diary:null,dream:null,plans:null,jobs:null},
            recordSequence:{},pending:Object.create(null),requests:Object.create(null),
            draft:null,message:'',messageKind:''};
          state.current='role-life';state.dirty=false;
          // 直接局部重画，绕开 render() 的切页动画延后分支。
          if(state.bundle)roleLifeRender();
        }""", copy.deepcopy(fixture))
        page.wait_for_selector("#section_role_life")

    def run(name, fn):
        try:
            ok, detail = fn()
            record(name, ok, detail)
        except Exception as error:  # 单项失败不中断整个套件
            record(name, False, error)

    # --- 1) 标签键盘操作（真正的 button + role=tab） ---
    def tabs_keyboard():
        reset(fixtures["normal"])
        page.get_by_role("tab", name="设置", exact=True).first.click()
        page.evaluate("()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
        switches = page.evaluate("document.querySelectorAll('#section_role_life .switch').length")
        native = page.evaluate(
            "document.querySelectorAll('#section_role_life button[role=switch]').length")
        if switches and native != switches:
            return False, f"开关不是原生 button/role=switch：{switches} 个中只有 {native} 个"
        page.get_by_role("tab", name="概览", exact=True).first.focus()
        page.keyboard.press("End")
        active = page.evaluate("roleLifeState.tab")
        page.keyboard.press("Home")
        home = page.evaluate("roleLifeState.tab")
        return active == "jobs" and home == "overview", (active, home)
    run("标签 End/Home 键盘可达", tabs_keyboard)

    # --- 2) 只读账号：设置控件、保存、写操作全部禁用 ---
    def read_only():
        reset(fixtures["disconnected"])
        page.get_by_role("tab", name="设置", exact=True).first.click()
        page.evaluate("()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
        enabled_controls = page.evaluate("""()=>[...document.querySelectorAll(
          '#content input:not([type=hidden]),#content select,#content textarea')]
          .filter(n=>n.getClientRects().length&&!n.disabled)
          .filter(n=>!['life-account','life-role'].includes(n.id)).length""")
        save_disabled = page.evaluate("""()=>{const b=document.getElementById('roleLifeSave');
          return b?b.disabled:null}""")
        return enabled_controls == 0 and save_disabled is not False, (enabled_controls, save_disabled)
    run("未连接/只读：控件与保存禁用", read_only)

    # --- 3) XSS：日记/梦/经历/任务错误/preset 名称一律转义 ---
    def xss():
        hostile = '<img src=x onerror="window.__xss=1">'
        sample = copy.deepcopy(fixtures["normal"])
        sample["presets"] = [{"id": "p1", "name": hostile}]
        sample["status"]["recent"][0]["text"] = hostile
        sample["status"]["jobs"][0]["error"] = hostile
        sample["status"]["metrics"]["last_error"] = hostile
        sample["status"]["plan"]["items"][0]["title"] = hostile
        sample["status"]["plan"]["items"][0]["purpose"] = hostile
        reset(sample)
        page.evaluate("window.__xss=undefined")
        page.get_by_role("tab", name="任务", exact=True).first.click()
        page.evaluate("()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
        injected = page.evaluate("document.querySelectorAll('#content img[src=x]').length")
        fired = page.evaluate("window.__xss")
        text_present = page.evaluate("document.getElementById('content').textContent.includes('<img src=x')")
        return injected == 0 and not fired and text_present, (injected, fired, text_present)
    run("XSS：生成文本/preset/job 错误被转义", xss)

    # --- 4) 草稿保护：切角色/离页需确认，取消后草稿仍在 ---
    def draft_guard():
        reset(fixtures["normal"])
        page.get_by_role("tab", name="设置", exact=True).first.click()
        page.evaluate("()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
        page.evaluate("roleLifeSetDraft('sleep_hours',9)")
        has = page.evaluate("roleLifeHasDraft()")
        page.evaluate("window.confirm=()=>false")
        before = page.evaluate("roleLifeState.account+'/'+roleLifeState.role")
        page.evaluate("void roleLifeSelect('20002','p9')")
        after = page.evaluate("roleLifeState.account+'/'+roleLifeState.role")
        still = page.evaluate("roleLifeHasDraft()")
        page.evaluate("window.confirm=()=>true")
        page.evaluate("void roleLifeSelect('20002','p9')")
        switched = page.evaluate("roleLifeState.account+'/'+roleLifeState.role")
        cleared = page.evaluate("roleLifeHasDraft()")
        return has and before == after and still and switched == "20002/p9" and not cleared, \
            (has, before, after, still, switched, cleared)
    run("草稿：取消切角色保留，确认后清空", draft_guard)

    # --- 5) 乱序响应：旧请求晚回不能覆盖新选择 ---
    def out_of_order():
        reset(fixtures["normal"])
        stale = copy.deepcopy(fixtures["normal"])
        stale["status"] = dict(stale["status"], state_version=1, date="STALE")
        fresh = copy.deepcopy(fixtures["normal"])
        fresh["status"] = dict(fresh["status"], state_version=99, date="FRESH")
        # 第一次请求挂起，第二次立即返回；随后放行第一次。
        page.evaluate("""([stale,fresh])=>{
          window.__pending=[];
          window.api=async()=>({data:null});
          let n=0;
          window.__resolve=[];
          window.api=()=>new Promise(res=>{
            n++; if(n===1){window.__resolve.push(()=>res(stale))}
            else res(fresh);
          });
        }""", [stale, fresh])
        page.evaluate("void loadRoleLife(true)")
        page.wait_for_timeout(100)
        page.evaluate("void loadRoleLife(true)")
        page.wait_for_timeout(200)
        page.evaluate("window.__resolve.forEach(f=>f())")
        page.wait_for_timeout(200)
        date = page.evaluate("roleLifeState.data?.status?.date")
        version = page.evaluate("roleLifeState.data?.status?.state_version")
        page.evaluate("delete window.api")
        return date == "FRESH" and version == 99, (date, version)
    run("乱序：旧响应不覆盖新状态", out_of_order)

    # --- 6) 失败重试复用同一个 request_id ---
    def idempotent_retry():
        reset(fixtures["normal"])
        page.evaluate("""()=>{
          window.__posts=[];
          window.api=async(url,opt)=>{window.__posts.push(JSON.parse(opt.body));
            if(window.__posts.length===1)throw Error('network');
            return {ok:true,data:{}}};
          window.toast=()=>{};
          window.loadRoleLife=async()=>{};
        }""")
        page.evaluate("void roleLifeAction('enqueue',{kind:'plan'})")
        page.wait_for_timeout(80)
        page.evaluate("void roleLifeAction('enqueue',{kind:'plan'})")
        page.wait_for_timeout(80)
        posts = page.evaluate("window.__posts")
        ids = [p.get("request_id") for p in posts]
        versions = [(p.get("expected_revision"), p.get("expected_epoch"),
                     p.get("expected_state_version")) for p in posts]
        page.evaluate("delete window.api;delete window.toast;delete window.loadRoleLife")
        return len(posts) == 2 and ids[0] and ids[0] == ids[1] and \
            versions == [("rev-1", 2, 7), ("rev-1", 2, 7)], (ids, versions)
    run("写操作：失败重试复用 request_id 与版本", idempotent_retry)

    # --- 7) 清空：必须输入“清空”，损坏时带 allow_corrupt ---
    def clear_confirm():
        reset(fixtures["normal"])
        page.evaluate("""()=>{window.__posts=[];window.toast=()=>{};window.loadRoleLife=async()=>{};
          window.api=async(url,opt)=>{window.__posts.push(JSON.parse(opt.body));return {ok:true,data:{}}};
          window.prompt=()=>'取消'}""")
        page.evaluate("void roleLifeClear()")
        page.wait_for_timeout(80)
        cancelled = page.evaluate("window.__posts.length")
        page.evaluate("window.prompt=()=>'清空'")
        page.evaluate("void roleLifeClear()")
        page.wait_for_timeout(120)
        posts = page.evaluate("window.__posts")
        normal_ok = cancelled == 0 and len(posts) == 1 and posts[0].get("action") == "clear" \
            and "allow_corrupt" not in posts[0]
        # 损坏状态只提供清空重建
        reset(fixtures["corrupt"])
        page.evaluate("""()=>{window.__posts=[];window.prompt=()=>'清空'}""")
        page.evaluate("void roleLifeClear()")
        page.wait_for_timeout(120)
        bad_posts = page.evaluate("window.__posts")
        corrupt_ok = len(bad_posts) == 1 and bad_posts[0].get("allow_corrupt") is True
        page.evaluate("delete window.api;delete window.toast;delete window.loadRoleLife;delete window.prompt")
        return normal_ok and corrupt_ok, (cancelled, posts, bad_posts)
    run("清空：二次确认 + 损坏 allow_corrupt", clear_confirm)

    # --- 8) 保存中：锁表单，不出现双击重复提交；保存后可继续编辑 ---
    def save_lock():
        reset(fixtures["normal"])
        page.get_by_role("tab", name="设置", exact=True).first.click()
        page.evaluate("()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
        page.evaluate("""()=>{window.__saveCalls=0;window.__release=null;
          window.api=(url,opt)=>new Promise(res=>{window.__saveCalls++;window.__release=()=>res(
            Object.assign({},roleLifeState.data,{config:JSON.parse(opt.body).config}))});
          window.toast=()=>{};}""")
        page.evaluate("roleLifeSetDraft('sleep_hours',9)")
        page.evaluate("void saveAll()")
        page.wait_for_timeout(80)
        saving = page.evaluate("roleLifeState.saving")
        disabled = page.evaluate("""()=>{const sw=document.getElementById('life-master-switch');
          const oldBar=document.querySelector('.life-savebar');
          const topSave=document.querySelector('.toolbar button[onclick="saveAll()"]');
          return !!sw && sw.disabled && !oldBar && (!topSave || !topSave.hidden);}""")
        # 保存中用户继续点保存不应再发一次
        page.evaluate("void saveAll()")
        page.wait_for_timeout(60)
        calls = page.evaluate("window.__saveCalls")
        page.evaluate("window.__release()")
        page.wait_for_timeout(150)
        saved = page.evaluate("roleLifeState.draft===null && !roleLifeState.saving && !state.dirty")
        # 保存完成后新增草稿必须保留，不被旧保存回包覆盖
        page.evaluate("roleLifeSetDraft('retention_days',30)")
        kept = page.evaluate("roleLifeState.draft && roleLifeState.draft.retention_days===30 && state.dirty")
        page.evaluate("delete window.api;delete window.toast")
        return saving and disabled and calls == 1 and saved and kept, (saving, disabled, calls, saved, kept)
    run("保存：锁表单/不双发/保存后新草稿保留", save_lock)

    # --- 9) 分页：只有一页时不出现可点的下一页 ---
    def pager():
        reset(fixtures["normal"])
        page.evaluate("""()=>{roleLifeState.tab='experiences';roleLifeState.subTab='activity';
          roleLifeState.records.activity={items:[{id:'e1',kind:'activity',text:'x',date:'2026-03-10'}],
            page:1,page_size:20,total:1,pages:1,loading:false,error:'',stale:false};
          roleLifeRender()}""")
        disabled = page.evaluate("""()=>{const buttons=[...document.querySelectorAll('#section_role_life button')];
          const next=buttons.find(b=>/下一页|下一頁|Next/i.test(b.textContent));
          return next?next.disabled:null}""")
        return disabled is not False, disabled
    run("分页：单页时不提供可点下一页", pager)

    # --- 10) 加载失败：明确错误与重试，不伪装损坏/空数据 ---
    def load_error():
        reset(fixtures["normal"])
        # 直接走“首次读取失败”分支：没有旧数据、错误已设置。
        page.evaluate("""()=>{roleLifeState.data=null;roleLifeState.selectorData=null;
          roleLifeState.loading=false;roleLifeState.error='离线临时失败';
          roleLifeRender();renderNav()}""")
        page.wait_for_timeout(80)
        text = page.evaluate("document.getElementById('content').textContent")
        has_retry = page.evaluate("""()=>[...document.querySelectorAll('#content button')]
          .some(b=>/重新读取|刷新|重试/.test(b.textContent))""")
        not_corrupt = "损坏" not in text and "不可读" not in text
        return "离线临时失败" in text and has_retry and not_corrupt, (text[:160], has_retry)
    run("加载失败：显示错误与重试，不标损坏", load_error)

    # --- 11) 睡眠/预算未知/空态/停用等状态都有可读文案 ---
    def states_copy():
        wanted = {
            "empty": ("还没有安排", "没有"),
            "disabled": ("开启", "生活"),
            "corrupt": ("不可读", "清空"),
            "budget_unknown": ("未知", "暂停"),
        }
        problems = []

        def text_of():
            return page.evaluate("document.getElementById('content').textContent")

        def click(tab):
            page.get_by_role("tab", name=tab, exact=True).first.click()
            page.evaluate("()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")

        reset(fixtures["empty"])
        click("日程")
        if "还没有安排" not in text_of():
            problems.append(("empty", text_of()[:160]))
        reset(fixtures["disabled"])
        if "角色生活已关闭" not in text_of():
            problems.append(("disabled", text_of()[:160]))
        reset(fixtures["corrupt"])
        if "状态不可读" not in text_of():
            problems.append(("corrupt", text_of()[:160]))
        reset(fixtures["budget_unknown"])
        if "未知" not in text_of():
            problems.append(("budget_unknown", text_of()[:160]))
        return not problems, problems
    run("状态文案：空/停用/损坏/预算未知", states_copy)

    # --- 12) 页面不横向溢出（手机 375 / 桌面 1280） ---
    def no_h_overflow():
        problems = []
        for name in ("normal", "long", "corrupt", "budget_unknown"):
            reset(fixtures[name])
            for width in (375, 1280):
                page.set_viewport_size({"width": width, "height": 900})
                for tab in ("概览", "日程", "生活记录", "设置", "任务"):
                    page.get_by_role("tab", name=tab, exact=True).first.click()
                    page.evaluate("()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
                    if page.evaluate("document.documentElement.scrollWidth>innerWidth"):
                        problems.append((name, width, tab,
                                         page.evaluate("document.documentElement.scrollWidth")))
        return not problems, problems[:6]
    run("几何：关键状态与标签无横向溢出", no_h_overflow)

    return checks


# ---------------------------------------------------------------- 主流程

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--phase", choices=("before", "after"), default="before")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--assert-clean", action="store_true")
    parser.add_argument("--shots-only", action="store_true",
                        help="只做 1280x900 / 375x844 明暗基线截图，不跑全矩阵")
    parser.add_argument("--quick", action="store_true",
                        help="只跑 normal 状态的 375/1280 明色矩阵，用于脚本自检")
    args = parser.parse_args()

    output_dir = Path(args.output_dir) if args.output_dir else Path(
        tempfile.mkdtemp(prefix=f"xcbot-role-life-{args.phase}-"))
    output_dir.mkdir(parents=True, exist_ok=True)

    src = load_sources()
    fixtures = state_fixtures()
    current = {"state": fixtures["normal"], "records": None}
    requests = []
    report = {"phase": args.phase, "output_dir": str(output_dir),
              "cases": [], "screenshots": [], "errors": []}

    def route(req):
        url = req.request.url
        requests.append(url)
        if not url.startswith("http://xcbot.test/"):
            raise AssertionError("非离线请求：" + url)
        if "/static/app.js" in url:
            req.fulfill(body=src["js"], content_type="text/javascript")
        elif "/static/app.css" in url:
            req.fulfill(body=src["css"], content_type="text/css")
        elif "/api/role-life/records" in url:
            raw = req.request.url.split("?", 1)
            qs = raw[1] if len(raw) > 1 else ""
            kind = re.search(r"(?:^|&)kind=([^&]*)", qs)
            kind = kind.group(1) if kind else "experiences"
            req.fulfill(json={"ok": True, "data": record_pages(kind)})
        elif "/api/role-life/config" in url:
            req.fulfill(json={"ok": True, "data": copy.deepcopy(current["state"])})
        elif "/api/role-life/action" in url:
            req.fulfill(json={"ok": True, "data": {"job_id": "offline-job"}})
        elif "/api/role-life" in url:
            req.fulfill(json={"ok": True, "data": copy.deepcopy(current["state"])})
        elif "/api/ui-state" in url:
            req.fulfill(json={"ok": True, "data": copy.deepcopy(src["bundle"])})
        elif "/api/auth/state" in url:
            req.fulfill(json={"ok": True, "data": {"has_token": True}})
        elif "/api/knowledge/documents" in url:
            req.fulfill(json={"ok": True, "data": []})
        elif "/api/plugins/schemas" in url:
            req.fulfill(json={"ok": True, "data": {}})
        elif "/api/plugins/store" in url:
            req.fulfill(json={"ok": True, "data": []})
        elif "/api/agent/mcp" in url:
            req.fulfill(json={"ok": True, "data": {"available": False, "servers": {},
                                                   "registered_tools": []}})
        elif "/assets/" in url:
            req.fulfill(status=204, body="")
        elif "/api/" in url:
            # 未知接口也必须由夹具应答；记录后返回空数据，绝不回落到真实后端。
            req.fulfill(json={"ok": True, "data": {}})
        else:
            req.fulfill(body=src["html"], content_type="text/html; charset=utf-8")

    def apply_visual(page, theme, glass):
        page.evaluate("""([theme,glass])=>{state.dirty=false;clearDraft();
          state.bundle.form_values['WebUI.liquid_glass']=glass;
          setTheme(theme);applyWebuiAppearance()}""", [theme, glass])

    def main_tabs(page):
        """主标签按 DOM 顺序取，不依赖具体文案（改版会换标签名）。"""
        return page.evaluate("""()=>{
          const list=document.querySelector('#content [role=tablist]');
          if(!list)return [];
          return [...list.querySelectorAll('[role=tab]')].map((n,i)=>n.textContent.trim());
        }""")

    def sub_tabs(page):
        return page.evaluate("""()=>{
          const lists=[...document.querySelectorAll('#content [role=tablist]')];
          if(lists.length<2)return [];
          return [...lists[1].querySelectorAll('[role=tab]')].map(n=>n.textContent.trim());
        }""")

    def click_tab(page, title):
        # 精确匹配，避免父标签文本包含子标签文字时误点。
        page.get_by_role("tab", name=title, exact=True).first.click()
        page.evaluate("()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")

    def safe(name):
        return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff-]+", "_", name)

    def shot(page, name, height=None):
        if height:
            page.set_viewport_size({"width": page.viewport_size["width"], "height": height})
        path = output_dir / f"{name}.png"
        page.screenshot(path=str(path))
        report["screenshots"].append(str(path))
        return path

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        context.route("**/*", route)
        context.add_init_script("localStorage.webuiToken='offline';")
        page = context.new_page()
        page_errors = []
        console_errors = []
        page.on("pageerror", lambda e: page_errors.append(str(e)))
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        page.goto("http://xcbot.test/#/role-life")
        page.wait_for_selector("#content .role-life, #section_role_life", timeout=15000)
        page.add_style_tag(content="*,*::before,*::after{animation:none !important;transition:none !important}")
        apply_visual(page, "light", False)

        # 1) 改前基线截图：桌面 1280x900、手机 375x844，明暗各一张概览。
        for theme in ("light", "dark"):
            apply_visual(page, theme, False)
            for width, height in ((1280, 900), (375, 844)):
                page.set_viewport_size({"width": width, "height": height})
                page.evaluate("document.querySelector('.main').scrollTop=0")
                page.evaluate("()=>new Promise(r=>requestAnimationFrame(()=>requestAnimationFrame(r)))")
                shot(page, f"baseline-{theme}-{width}")

        if args.shots_only:
            page.set_viewport_size({"width": 1280, "height": 900})
            # 加一张完整内容的桌面长图，便于人工看层级与空白。
            apply_visual(page, "light", False)
            shot(page, "baseline-light-1280-tall", 2200)
            apply_visual(page, "dark", False)
            shot(page, "baseline-dark-1280-tall", 2200)
        else:
            # 2) 状态矩阵 × 标签 × 宽度 × 主题 × 玻璃。几何检查固定用 900 高。
            # 标签名按运行时真实 DOM 取；改版换文案不影响脚本。
            widths = (375, 768, 981, 1280)
            matrix = fixtures
            if args.quick:
                widths = (375, 1280)
                matrix = {"normal": fixtures["normal"]}
            for state_name, fixture in matrix.items():
                current["state"] = fixture
                page.evaluate("""s=>{roleLifeState.data=s;roleLifeState.loading=false;
                  roleLifeState.error='';roleLifeState.account=s.account||'';
                  roleLifeState.role=s.role_id||'';roleLifeState.draft=null;
                  roleLifeState.message='';roleLifeState.tab='overview';
                  roleLifeState.subTab='activity';
                  roleLifeState.records={activity:null,diary:null,dream:null,plans:null,jobs:null};
                  roleLifeState.page={activity:1,diary:1,dream:1,plans:1,jobs:1};
                  render()}""", copy.deepcopy(fixture))
                page.wait_for_selector("#content .role-life, #section_role_life")
                tabs = main_tabs(page)
                for theme in ("light", "dark"):
                    for glass in (False, True):
                        apply_visual(page, theme, glass)
                        for width in widths:
                            page.set_viewport_size({"width": width, "height": 900})
                            for title in tabs:
                                click_tab(page, title)
                                subs = sub_tabs(page)
                                if subs:
                                    for sub in subs:
                                        click_tab(page, sub)
                                        report["cases"].append(
                                            {"state": state_name,
                                             "tab": f"{title}/{sub}",
                                             "width": width, "theme": theme,
                                             "glass": glass,
                                             "findings": audit(page)})
                                else:
                                    report["cases"].append(
                                        {"state": state_name, "tab": title,
                                         "width": width, "theme": theme,
                                         "glass": glass,
                                         "findings": audit(page)})
                            # 每种状态在 375/1280、明暗各留一张首标签截图
                            if width in (375, 1280):
                                click_tab(page, tabs[0])
                                shot(page, f"{safe(state_name)}-{theme}-glass{int(glass)}-{width}")
                        page.set_viewport_size({"width": 320, "height": 900})
            # 3) 关键页面的长图（真实内容高度），用于人工判读排版。
            current["state"] = fixtures["normal"]
            page.evaluate("""s=>{roleLifeState.data=s;roleLifeState.loading=false;
              roleLifeState.error='';roleLifeState.account=s.account||'';
              roleLifeState.role=s.role_id||'';roleLifeState.draft=null;
              roleLifeState.tab='overview';roleLifeState.subTab='activity';
              roleLifeState.records={activity:null,diary:null,dream:null,plans:null,jobs:null};
              roleLifeState.page={activity:1,diary:1,dream:1,plans:1,jobs:1};
              render()}""", copy.deepcopy(fixtures["normal"]))
            page.wait_for_selector("#content .role-life, #section_role_life")
            tabs = main_tabs(page)
            for theme in ("light", "dark"):
                apply_visual(page, theme, False)
                for width, height, suffix in ((1280, 900, "1280-tall"),
                                              (375, 844, "375-tall")):
                    page.set_viewport_size({"width": width, "height": height})
                    click_tab(page, tabs[0])
                    shot(page, f"normal-{theme}-{suffix}", 2200)
                    if len(tabs) > 1:
                        # 设置页通常是最后一个或标记为“设置”的标签
                        settings = next((t for t in tabs if t == "设置"), tabs[-1])
                        click_tab(page, settings)
                        shot(page, f"settings-{theme}-{suffix}", 2400)

            # 4) 交互套件：草稿保护、乱序响应、失败重试同 request_id、
            #    开关键盘、清空确认、XSS、只读禁用、无无效分页。
            page.set_viewport_size({"width": 1280, "height": 900})
            checks = interaction_checks(page, current, fixtures, src)
            report["interaction"] = checks

        report["page_errors"] = page_errors
        report["console_errors"] = console_errors
        report["all_offline"] = all(u.startswith("http://xcbot.test/") for u in requests)
        report["request_count"] = len(requests)
        context.close()
        browser.close()

    issues = summarize(report)
    (output_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"截图目录：{output_dir}")
    print(f"请求 {report['request_count']} 次，全部离线：{report['all_offline']}")
    print(f"pageerror {len(report['page_errors'])} 条，console error {len(report['console_errors'])} 条")
    for err in report["page_errors"][:5]:
        print("  pageerror:", err)
    for err in report["console_errors"][:5]:
        print("  console:", err)
    for line in issues:
        print(line)
    for check in report.get("interaction", []):
        print(("PASS " if check["ok"] else "FAIL ") + check["name"]
              + ("" if check["ok"] else f"  -> {check['detail']}"))
    if args.assert_clean:
        must_fix = [i for i in issues if i.startswith(("[溢出]", "[越界]", "[重复转义]"))]
        assert not must_fix, must_fix
        assert not page_errors, page_errors
        failed = [c for c in report.get("interaction", []) if not c["ok"]]
        assert not failed, failed
    if args.phase == "after":
        print("提示：after 阶段请人工查看截图后再判定视觉结论。")


def summarize(report):
    lines = []
    overflow = [c for c in report["cases"] if c["findings"].get("overflow")]
    if overflow:
        sample = overflow[0]
        lines.append(f"[溢出] {len(overflow)} 个组合横向溢出，例如 "
                     f"{sample['state']}/{sample['tab']}/{sample['width']}/{sample['theme']}"
                     f"（docWidth={sample['findings']['docWidth']}）")
    out_of_bounds = [c for c in report["cases"] if c["findings"].get("outOfBounds")]
    if out_of_bounds:
        sample = out_of_bounds[0]
        lines.append(f"[越界] {len(out_of_bounds)} 个组合有元素越出 .main，例如 "
                     f"{sample['state']}/{sample['tab']}/{sample['width']}："
                     f"{sample['findings']['outOfBounds'][:2]}")
    clipped = [c for c in report["cases"] if c["findings"].get("clipped")]
    if clipped:
        sample = clipped[0]
        lines.append(f"[裁切] {len(clipped)} 个组合有元素被祖先裁切，例如 "
                     f"{sample['state']}/{sample['tab']}/{sample['width']}："
                     f"{sample['findings']['clipped'][:2]}")
    overlaps = [c for c in report["cases"] if c["findings"].get("overlap")]
    if overlaps:
        sample = overlaps[0]
        lines.append(f"[重叠] {len(overlaps)} 个组合出现控件/卡片重叠，例如 "
                     f"{sample['state']}/{sample['tab']}/{sample['width']}："
                     f"{sample['findings']['overlap'][:2]}")
    small = [c for c in report["cases"] if c["findings"].get("smallControls")]
    if small:
        sample = small[0]
        lines.append(f"[小控件] {len(small)} 个组合有小于 24px 的可点击目标，例如 "
                     f"{sample['findings']['smallControls'][:3]}")
    blocked = [c for c in report["cases"] if c["findings"].get("blocked")]
    if blocked:
        sample = blocked[0]
        lines.append(f"[遮挡] {len(blocked)} 个组合有控件中心点被其它元素遮挡，例如 "
                     f"{sample['state']}/{sample['tab']}/{sample['width']}/{sample['theme']}"
                     f"/glass{sample['glass']}：{sample['findings']['blocked'][:3]}")
    clipped_content = [c for c in report["cases"] if c["findings"].get("clippedContent")]
    if clipped_content:
        sample = clipped_content[0]
        lines.append(f"[内容裁切] {len(clipped_content)} 个组合的卡片内容被 overflow 裁掉，例如 "
                     f"{sample['state']}/{sample['tab']}/{sample['width']}："
                     f"{sample['findings']['clippedContent'][:2]}")
    sticky = [c for c in report["cases"] if c["findings"].get("stickyCovered")]
    if sticky:
        sample = sticky[0]
        lines.append(f"[保存条遮挡] {len(sticky)} 个组合里粘性保存条盖住正文，例如 "
                     f"{sample['state']}/{sample['tab']}/{sample['width']}："
                     f"{sample['findings']['stickyCovered'][:2]}")
    escaped = [c for c in report["cases"] if c["findings"].get("escapedTags")]
    if escaped:
        sample = escaped[0]
        lines.append(f"[重复转义] {len(escaped)} 个组合把 HTML 源码显示成文字，例如 "
                     f"{sample['state']}/{sample['tab']}：{sample['findings']['escapedTags'][:2]}")
    nowrap = [c for c in report["cases"] if c["findings"].get("nowrapLong")]
    if nowrap:
        lines.append(f"[换行丢失] {len(nowrap)} 个组合里含换行的正文未使用 pre-wrap")
    primary = [c for c in report["cases"] if c["findings"].get("primaryButtons", 0) > 1]
    if primary:
        sample = primary[0]
        lines.append(f"[主按钮] {len(primary)} 个组合同屏有多个 .btn.primary，例如 "
                     f"{sample['state']}/{sample['tab']}：{sample['findings']['primaryButtons']} 个")
    switches = [c for c in report["cases"] if c["findings"].get("switchNotButton")]
    if switches:
        sample = switches[0]
        lines.append(f"[开关元素] 使用 div.switch 而非原生 button/role=switch，例如 "
                     f"{sample['state']}/{sample['tab']}：{sample['findings']['switchNotButton']} 个")
    tabs = report["cases"][0]["findings"].get("tabs") if report["cases"] else []
    if tabs:
        lines.append(f"[标签] 主标签 {len(tabs)} 个："
                     + "、".join(t["text"] for t in tabs))
    if not lines:
        lines.append("[通过] 矩阵内未发现溢出、越界、裁切、重叠或重复转义。")
    return lines


if __name__ == "__main__":
    main()

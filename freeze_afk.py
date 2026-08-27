#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FreezeHost AFK - 自动挂机赚币脚本（修复版 v2.1）
=================================================
v2.1 相对 v2.0 的改动（依据你 GHA 实测日志）：

[致命BUG] 代理回退永远不生效
  * 日志证据：触发"切换为直连"后进程直接"运行结束！"，没有重建浏览器。
  * 根因：SeleniumBase 的 SB() 上下文管理器在 test=True 模式下会【吞掉】
    with 块内的所有异常（seleniumbase/plugins/sb_manager.py:1402-1408:
    `if (test or inner_test) and not test_name: print(e); return`）。
    我上一版用 `raise DriverDied` 跳出 SB 块，异常被吞，外层重建逻辑
    永远收不到信号。
  * 修复：改用纯标志位控制流（need_restart + break 正常退出 with 块），
    不再依赖任何异常穿越 SB 边界；浏览器重建/代理切换由外层 while 执行。

[新增能力]
  * TRY_SOLVE=1：点击复选框后额外尝试 uc_gui_handle_captcha() 解图片挑战
    （默认 0，保守）。
  * RUSH_AFK_ON_FAIL=1（默认开）：验证码超时后，若 Start AFK 按钮实际可点，
    先点一次并检查 WebSocket——部分情况下按钮不校验 token 也能开始赚币。
  * 点击阶段每 10 秒输出验证码部件状态诊断（iframe 是否存在 / 是否出现
    交互式图片挑战 #challenge-stage / response 长度），下次失败截图+日志
    能直接看出挑战停在什么状态。
  * UC_CDP=1：切换 SeleniumBase CDP 模式（uc_cdp=True），该模式下验证码
    点击走 CDP Input 事件而非 pyautogui 屏幕坐标，Xvfb 下更稳（默认 0）。
  * earn.yml 增加失败截图上传 artifacts，失败后可直接下载图片排查。
"""
import os
import time
import sys
import platform
import tempfile

# ---------------------------------------------------------------------------
# 环境变量配置
# ---------------------------------------------------------------------------
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
WARP_PROXY = os.environ.get("WARP_PROXY", "socks5://127.0.0.1:40000")
MAX_RUNTIME = int(os.environ.get("MAX_RUNTIME", "0"))
SESSION_DURATION = int(os.environ.get("SESSION_DURATION", "1200"))
INSTANCE_ID = int(os.environ.get("INSTANCE_ID", "0"))
LOG_FILE = os.environ.get("LOG_FILE", "")
PROXY_MODE = os.environ.get("PROXY_MODE", "auto")          # warp | direct | auto
# 代理顺序（auto 模式循环用），默认直连优先（GHA 上 WARP 常挂）
_raw_order = [p.strip() for p in os.environ.get("PROXY_ORDER", "").split(",") if p.strip()]
if _raw_order:
    PROXY_ORDER = _raw_order
elif PROXY_MODE == "warp":
    PROXY_ORDER = ["warp"]
elif PROXY_MODE == "direct":
    PROXY_ORDER = ["direct"]
else:
    PROXY_ORDER = ["direct", "warp"]
TRY_SOLVE = os.environ.get("TRY_SOLVE", "0") == "1"        # 解图片挑战
RUSH_AFK_ON_FAIL = os.environ.get("RUSH_AFK_ON_FAIL", "1") == "1"  # 失败后仍试按 Start AFK
UC_CDP = os.environ.get("UC_CDP", "0") == "1"              # 用 CDP 模式

TURNSTILE_QUIET = int(os.environ.get("TURNSTILE_QUIET", "25"))
CLICK_GAP = int(os.environ.get("CLICK_GAP", "18"))
MAX_CLICKS = int(os.environ.get("MAX_CLICKS", "3"))
CHALLENGE_TIMEOUT = int(os.environ.get("CHALLENGE_TIMEOUT", "150"))
PROXY_FAIL_SWITCH = int(os.environ.get("PROXY_FAIL_SWITCH", "2"))
BROWSER_RESTART_MAX = int(os.environ.get("BROWSER_RESTART_MAX", "8"))
RETRY_DELAY = int(os.environ.get("RETRY_DELAY", "10"))

TURNSTILE_IFRAME = 'iframe[src*="challenges.cloudflare.com"]'

# Linux 虚拟显示器（GitHub Actions 必需）
if platform.system().lower() == "linux":
    try:
        from pyvirtualdisplay import Display
        disp = Display(visible=False, size=(1920, 1080))
        disp.start()
        os.environ["DISPLAY"] = disp.new_display_var
        print("[环境] 已启动 Xvfb 虚拟显示器")
    except Exception as e:
        print("[环境] pyvirtualdisplay 启动失败（若已有 DISPLAY 可忽略）: %s" % e)

from seleniumbase import SB

try:
    from asyncio import CancelledError as _CancelledError
except ImportError:
    _CancelledError = Exception

global_start = time.time()


def log(msg):
    ts = time.strftime("%H:%M:%S")
    prefix = "[I%d] " % INSTANCE_ID if INSTANCE_ID else ""
    line = "[%s] %s%s" % (ts, prefix, msg)
    print(line, flush=True)
    if LOG_FILE:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def elapsed():
    return time.time() - global_start


def runtime_exceeded():
    return MAX_RUNTIME > 0 and elapsed() > MAX_RUNTIME * 60


# ---------------------------------------------------------------------------
# Turnstile 工具
# ---------------------------------------------------------------------------
def _turnstile_value(sb):
    try:
        v = sb.execute_script(
            "var t=document.querySelector('[name=cf-turnstile-response]');"
            "return t ? (t.value || '') : '';"
        ) or ""
        return str(v)
    except BaseException:
        return ""


def dump_page_state(sb, tag):
    """页面全状态转储 v2.3：URL/标题/正文/iframe/按钮(含id class)/链接/关键元素"""
    try:
        j = sb.execute_script(
            "var o={};"
            "o.url=(location.href||'').substring(0,160);"
            "o.title=(document.title||'').substring(0,100);"
            "o.text=(document.body?document.body.innerText:'')."
            "replace(/\\s+/g,' ').substring(0,900);"
            "var fs=document.querySelectorAll('iframe'),srcs=[];"
            "for(var i=0;i<fs.length&&i<10;i++){"
            "srcs.push((fs[i].src||fs[i].name||'na').substring(0,120));}"
            "o.iframes=srcs;"
            "o.start_afk=!!document.getElementById('start-afk-btn');"
            "o.login_btn=!!document.getElementById('login-btn');"
            "o.cf=!!document.querySelector('.cf-turnstile,.cf-turnstile-wrapper,"
            "[class*=\"turnstile\"],iframe[src*=\"challenges.cloudflare.com\"],"
            "iframe[src*=\"challenge-platform\"],[name*=cf-turnstile]');"
            "o.adb=!!document.getElementById('freeze-adblock-blocker');"
            "o.adb_active=!!document.querySelector('#freeze-adblock-blocker.active');"
            "o.adb_flag=(window.adblockerDetected===true);"
            "o.ws=(typeof ws!=='undefined'&&ws)?ws.readyState:-1;"
            "var btns=[],all=document.querySelectorAll('button');"
            "for(var j=0;j<all.length&&j<15;j++){"
            "var t=(all[j].innerText||'').trim().replace(/\\s+/g,' ').substring(0,40);"
            "var id=(all[j].id||'');var cl=(all[j].className||'').toString().substring(0,40);"
            "if(t||id)btns.push((t||'?')+'|#'+id+'|.'+cl);}"
            "o.buttons=btns;"
            "var as=[],aa=document.querySelectorAll('a');"
            "for(var k=0;k<aa.length&&k<15;k++){"
            "var t=(aa[k].innerText||'').trim().replace(/\\s+/g,' ').substring(0,25);"
            "var h=(aa[k].getAttribute('href')||'').substring(0,60);"
            "if(t||h)as.push(t+'|'+h);}"
            "o.links=as;"
            "return JSON.stringify(o);"
        )
        log("  [页面状态 %s] %s" % (tag, str(j)[:2200]))
    except BaseException as e:
        log("  [页面状态 %s] 获取失败: %s" % (tag, str(e)[:100]))


def _proxy_connection_error(sb):
    """页面是否为 Chrome 代理错误页（WARP 挂掉时的典型表现）"""
    try:
        return bool(sb.execute_script(
            "return (location.protocol==='chrome-error:'||"
            "(document.body&&document.body.innerText.indexOf('ERR_')>=0&&"
            "document.body.innerText.indexOf('proxy')>=0))?true:false;"
        ))
    except BaseException:
        return False


def _click_acknowledge(sb):
    """自动点掉站点公告/consent 弹窗（如 Acknowledge/OK/Accept）"""
    try:
        clicked = sb.execute_script(
            "var els=document.querySelectorAll('button,a,[role=button]');"
            "for(var i=0;i<els.length;i++){"
            "var t=(els[i].innerText||'').toLowerCase();"
            "if(t.indexOf('acknowledge')>=0||t.indexOf('agree')>=0||"
            "t.indexOf('accept')>=0||t.indexOf('ok')===0||t.indexOf('i understand')>=0){"
            "els[i].click();return els[i].tagName+':'+(els[i].innerText||'').trim().substring(0,30);}}"
            "return '';"
        )
        if clicked:
            log("已点击公告/consent 控制: %s" % str(clicked)[:60])
        return bool(clicked)
    except BaseException:
        return False


def bypass_adblock(sb):
    """提早绕过站点反广告拦截：伪造探针 + 清遮罩 + 守护定时器"""
    try:
        sb.execute_script("""
window.adblockerDetected=false;window._adblockerDetected=false;
if(!window.__fhFetchShim){
  window.__fhFetchShim=true;
  var _of=window.fetch;
  window.fetch=function(u,o){
    var s=String(u);
    if(s.indexOf('/assets/js/advertisement')>=0||s.indexOf('pagead2.googlesyndication.com')>=0){
      return Promise.resolve({ok:true,status:200,statusText:'OK'});
    }
    return _of.apply(this,arguments);
  };
  window.checkAdblocker=function(){return Promise.resolve(false);};
  setInterval(function(){
    window.adblockerDetected=false;window._adblockerDetected=false;
    var x=document.getElementById('freeze-adblock-blocker');
    if(x&&x.classList.contains('active')){x.classList.remove('active');x.style.display='none';}
    var s=document.getElementById('freeze-adblock-lock-style');if(s)s.remove();
  },1000);
}
var b=document.getElementById('freeze-adblock-blocker');
if(b){b.classList.remove('active');b.style.display='none';}
""")
        log("反广告拦截绕过已注入")
    except BaseException as e:
        log("反广告拦截绕过注入失败: %s" % str(e)[:100])


def _widget_state(sb):
    """返回验证码部件状态快照，用于诊断"""
    try:
        return sb.execute_script(
            "var o={};"
            "o.iframe=!!document.querySelector('iframe[src*=\"challenges.cloudflare.com\"]');"
            "o.wrapper=!!document.querySelector('.cf-turnstile, .cf-turnstile-wrapper, [class*=\"turnstile\"]');"
            "o.stage=!!document.querySelector('#challenge-stage');"
            "var t=document.querySelector('[name=cf-turnstile-response]');"
            "o.resp=(t&&t.value)?t.value.length:0;"
            "return JSON.stringify(o);"
        )
    except BaseException:
        return "{}"


def click_turnstile_once(sb):
    try:
        if sb.is_element_present(TURNSTILE_IFRAME):
            ok = sb.uc_gui_click_captcha(frame=TURNSTILE_IFRAME)
        else:
            ok = sb.uc_gui_click_captcha()
        log("已点击 Turnstile 复选框" if ok else "Turnstile 点击未生效")
        return bool(ok)
    except BaseException as e:
        log("Turnstile 点击异常: %s" % str(e)[:120])
        return False


def wait_turnstile(sb, timeout=CHALLENGE_TIMEOUT, home="https://free.freezehost.pro"):
    """返回验证码 token；失败返回 None"""
    start = time.time()
    last_click = 0.0
    click_count = 0
    solved_tried = False
    last_diag = 0.0

    def _still_on_site():
        """等待期间页面若被踢走（如跳去 Discord OAuth），立刻上报并短路"""
        try:
            u = sb.get_current_url()
            if not u.startswith(home):
                log("等待期间页面跳离站点: %s" % u[:100])
                dump_page_state(sb, "跳离站点")
                return False
        except BaseException:
            pass
        return True

    def _diag(tag):
        nonlocal last_diag
        now = time.time()
        if now - last_diag >= 10:
            last_diag = now
            log("  [验证码状态] %s → %s" % (tag, _widget_state(sb)))
            # 静置期 30s/60s 整点做一次完整页面转储（捕捉延迟渲染）
            el = now - start
            if el >= 30 and el < 32:
                dump_page_state(sb, "静置30s")
            elif el >= 60 and el < 62:
                dump_page_state(sb, "静置60s")

    # 阶段1：静置等待（托管型自动通过）
    quiet_end = min(start + TURNSTILE_QUIET, start + timeout)
    while time.time() < quiet_end:
        v = _turnstile_value(sb)
        if v and len(v) > 20:
            log("Turnstile 自动通过！（静置方式）")
            return v
        if not _still_on_site():
            return None
        _diag("静置中")
        time.sleep(2)

    # 阶段2：按需精准点击
    log("静置 %ds 未通过，开始按需精准点击（最多 %d 次、间隔 %ds）"
        % (TURNSTILE_QUIET, MAX_CLICKS, CLICK_GAP))
    while time.time() - start < timeout:
        v = _turnstile_value(sb)
        if v and len(v) > 20:
            log("Turnstile 验证通过！")
            return v
        if runtime_exceeded():
            return None
        if not _still_on_site():
            return None
        if click_count < MAX_CLICKS and (time.time() - last_click) >= CLICK_GAP:
            click_count += 1
            last_click = time.time()
            click_turnstile_once(sb)
            # 点击后尝试解交互式图片挑战（可选）
            if TRY_SOLVE and not solved_tried:
                solved_tried = True
                try:
                    log("尝试自动处理交互式挑战…")
                    sb.uc_gui_handle_captcha()
                    log("uc_gui_handle_captcha 执行完毕")
                except BaseException as e:
                    log("自动解挑战失败: %s" % str(e)[:100])
        else:
            _diag("等待中")
            time.sleep(3)
    return None


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------
def login_via_discord_token(sb, token):
    log("打开 FreezeHost…")
    sb.uc_open_with_reconnect("https://free.freezehost.pro", reconnect_time=5)
    time.sleep(5)

    try:
        sb.click("button#login-btn")
    except Exception:
        sb.execute_script("document.getElementById('login-btn')?.click();")
    time.sleep(3)

    try:
        sb.wait_for_element_visible("button#confirm-login", timeout=5)
        sb.click("button#confirm-login")
        log("已确认服务条款")
    except Exception:
        log("无服务条款弹窗")
    time.sleep(2)

    if "discord.com" in sb.get_current_url():
        log("注入 Token…")
        sb.execute_script("""(function(){
            var token = "%s";
            var f = document.createElement("iframe");
            f.style.display = "none";
            document.body.appendChild(f);
            try { f.contentWindow.localStorage.setItem("token", '"'+token+'"'); } catch(e) {}
            try { localStorage.setItem("token", '"'+token+'"'); } catch(e) {}
            document.body.removeChild(f);
        })();""" % token)

        log("刷新页面等待 OAuth 回调…")
        sb.driver.refresh()
        time.sleep(8)

        url = sb.get_current_url()
        if "discord.com/login" in url:
            log("Token 无效！")
            return False

        if "discord.com/oauth2" in url:
            log("自动授权…")
            sb.execute_script("""(function(){
                document.querySelectorAll("button").forEach(function(btn){
                    if(btn.textContent.toLowerCase().includes("authorize")) btn.click();
                });
            })();""")
            time.sleep(5)

        for _ in range(20):
            url = sb.get_current_url()
            if url.startswith("https://free.freezehost.pro"):
                break
            time.sleep(2)

    url = sb.get_current_url()
    log("当前地址: %s" % url)
    return url.startswith("https://free.freezehost.pro")


# ---------------------------------------------------------------------------
# 绕过广告拦截 + 点击 Start AFK
# ---------------------------------------------------------------------------
def click_start_afk(sb):
    log("绕过广告拦截检测…")
    try:
        sb.execute_script("""
            if(typeof adblockerDetected !== 'undefined') adblockerDetected = false;
            var msg = document.getElementById('adblocker-message');
            if(msg) msg.style.display = 'none';
        """)
    except Exception:
        pass
    try:
        sb.execute_script("""
            var btn = document.getElementById('start-afk-btn');
            if(btn){ btn.disabled = false; btn.textContent = 'Start AFK Session'; }
        """)
    except Exception:
        pass

    for attempt in range(3):
        try:
            sb.wait_for_element_visible("#start-afk-btn", timeout=5)
            sb.click("#start-afk-btn")
            log("已点击 Start AFK！")
            time.sleep(3)
            ws_state = sb.execute_script(
                "return (typeof ws !== 'undefined' && ws) ? ws.readyState : -1;"
            )
            log("WebSocket 状态: %s" % ws_state)
            if ws_state in (0, 1):
                return True
        except BaseException as e:
            log("第 %d 次点击异常: %s" % (attempt + 1, str(e)[:80]))
            try:
                sb.execute_script("""
                    if(typeof adblockerDetected !== 'undefined') adblockerDetected = false;
                    document.getElementById('start-afk-btn')?.click();
                """)
                time.sleep(3)
                ws_state = sb.execute_script(
                    "return (typeof ws !== 'undefined' && ws) ? ws.readyState : -1;"
                )
                log("JS 兜底点击 - WS 状态: %s" % ws_state)
                if ws_state in (0, 1):
                    return True
            except BaseException:
                pass
    return False


# ---------------------------------------------------------------------------
# 单个赚币 session（返回 True=成功 / False=失败 / None=到达时长 / "dead"=浏览器死亡）
# ---------------------------------------------------------------------------
def run_earn_session(sb, session_num, token, hard_deadline):
    afk_started = False  # RUSH 模式下已成功点击过按钮则不再二次点击
    log("加载 /earn…")
    sb.uc_open_with_reconnect("https://free.freezehost.pro/earn", reconnect_time=6)
    time.sleep(15)
    bypass_adblock(sb)  # 提早绕过反广告锁，避免赚钱 UI 不被渲染
    dump_page_state(sb, "earn加载后")

    if _proxy_connection_error(sb):
        log("代理连接失败（chrome-error 页）→ 本轮判代理死亡")
        return "proxy_dead"

    url = sb.get_current_url()
    if not url.startswith("https://free.freezehost.pro"):
        log("会话过期，重新登录…")
        if not login_via_discord_token(sb, token):
            return False
        sb.uc_open_with_reconnect("https://free.freezehost.pro/earn", reconnect_time=6)
        time.sleep(15)
        bypass_adblock(sb)
        dump_page_state(sb, "earn加载后(重登)")
        if _proxy_connection_error(sb):
            log("重登后仍为代理错误页 → 本轮判代理死亡")
            return "proxy_dead"

    # 自动点掉公告/consent 弹窗（若存在）
    if _click_acknowledge(sb):
        time.sleep(4)
        dump_page_state(sb, "点ack后")

    log("等待 Turnstile（最多 %ds）…" % CHALLENGE_TIMEOUT)
    token_val = wait_turnstile(sb, timeout=CHALLENGE_TIMEOUT)
    if token_val is None:
        if runtime_exceeded() or (hard_deadline and time.time() > hard_deadline):
            log("已达到最大运行时长")
            return None
        log("Turnstile 验证失败！")
        dump_page_state(sb, "失败时")
        try:
            shot = os.path.join(tempfile.gettempdir(),
                                "fh_fail_%d_%d.png" % (INSTANCE_ID, session_num))
            sb.save_screenshot(shot)
            log("失败截图已保存: %s" % shot)
        except BaseException:
            pass
        # 兜底：验证码没拿到，但按钮存在就强制点一次试试
        # （部分情况下 Start AFK 不校验 token；click_start_afk 自带绕过 disabled）
        if RUSH_AFK_ON_FAIL and sb.is_element_present("#start-afk-btn"):
            log("[RUSH_AFK_ON_FAIL] 强制点击 Start AFK 试试…")
            if click_start_afk(sb):
                log("按钮点击成功且 WebSocket 已建立 → 继续赚币（无 token 模式）")
                afk_started = True
            else:
                log("按钮点击后未建立连接，本 session 判失败")
                return False
        else:
            log("验证码未通过且按钮不存在/已关闭 RUSH，本 session 判失败")
            return False

    if not afk_started and not click_start_afk(sb):
        log("警告：Start AFK 按钮点击失败，继续观察 20 秒…")
        time.sleep(20)
        try:
            ws_state = sb.execute_script(
                "return (typeof ws !== 'undefined' && ws) ? ws.readyState : -1;"
            )
        except BaseException:
            ws_state = -1
        if ws_state not in (0, 1):
            log("WebSocket 未建立，本 session 判失败")
            return False

    log("开始赚币 %d 秒…" % SESSION_DURATION)
    session_start = time.time()
    while time.time() - session_start < SESSION_DURATION:
        if runtime_exceeded() or (hard_deadline and time.time() > hard_deadline):
            log("达到最大运行时长，提前结束")
            return None
        try:
            url = sb.get_current_url()
            if not url.startswith("https://free.freezehost.pro"):
                log("赚币期间会话过期")
                break
            # 每 60 秒确认一次 WebSocket 活着
            ws_state = sb.execute_script(
                "return (typeof ws !== 'undefined' && ws) ? ws.readyState : -1;"
            )
            if ws_state not in (0, 1):
                log("赚币期间 WebSocket 断开（状态 %s），提前结束" % ws_state)
                break
        except BaseException as e:
            log("会话检查异常（可能是浏览器问题）: %s" % str(e)[:100])
            return "dead"
        time.sleep(30)

    log("Session #%d 完成" % session_num)
    return True


# ---------------------------------------------------------------------------
# 主循环：session + 浏览器重建 + 代理回退（纯标志位控制流）
# ---------------------------------------------------------------------------
def build_options(proxy):
    opts = {
        "uc": True,
        "test": True,
        "headed": True,
        "chromium_arg": (
            "--no-sandbox,--disable-dev-shm-usage,--disable-gpu,"
            "--disable-background-networking,--disable-component-update,"
            "--disable-software-rasterizer,--window-size=1280,720"
        ),
    }
    if proxy:
        opts["proxy"] = proxy
    if UC_CDP:
        opts["uc_cdp"] = True
    return opts


def driver_alive(sb):
    try:
        sb.execute_script("return 1;")
        return True
    except BaseException:
        return False


def main():
    if not DISCORD_TOKEN:
        print("ERROR: DISCORD_TOKEN 未设置！")
        print("设置方式: export DISCORD_TOKEN='你的token'")
        return

    tokens = [t.strip() for t in DISCORD_TOKEN.split(",") if t.strip()]
    token = tokens[INSTANCE_ID % len(tokens)]

    log("=" * 56)
    log("FreezeHost AFK 修复版 v2.3 - 实例 #%d" % INSTANCE_ID)
    log("Token: %s...%s" % (token[:10], token[-5:]))
    log("代理顺序: %s TRY_SOLVE=%s RUSH_AFK_ON_FAIL=%s UC_CDP=%s"
        % (PROXY_ORDER, TRY_SOLVE, RUSH_AFK_ON_FAIL, UC_CDP))
    log("=" * 56)

    def _proxy_value(name):
        return None if name == "direct" else (WARP_PROXY if name == "warp" else name)

    proxy_cycle = list(PROXY_ORDER)
    proxy_idx = 0
    proxy = _proxy_value(proxy_cycle[0])

    def _next_proxy():
        nonlocal proxy_idx, proxy
        proxy_idx += 1
        proxy = _proxy_value(proxy_cycle[proxy_idx % len(proxy_cycle)])
        return proxy

    hard_deadline = (global_start + MAX_RUNTIME * 60) if MAX_RUNTIME > 0 else 0

    browser_restarts = 0
    session = 0
    tf_fail_in_row = 0  # 连续 Turnstile 失败数（驱动崩溃不计入）

    while True:
        if runtime_exceeded() or (hard_deadline and time.time() > hard_deadline):
            log("达到最大运行时长，结束")
            break
        if browser_restarts > BROWSER_RESTART_MAX:
            log("浏览器重建次数超过上限（%d），结束" % BROWSER_RESTART_MAX)
            break

        need_restart = False
        restart_reason = ""

        log("启动浏览器…（代理: %s，实例 %d/%d）"
            % (proxy or "直连", browser_restarts + 1, BROWSER_RESTART_MAX))
        try:
            with SB(**build_options(proxy)) as sb:
                # ---- 登录 ----
                try:
                    login_ok = login_via_discord_token(sb, token)
                except BaseException as e:
                    log("登录阶段异常: %s" % str(e)[:120])
                    login_ok = False

                if not login_ok:
                    if not driver_alive(sb):
                        need_restart = True
                        restart_reason = "登录时浏览器死亡"
                    else:
                        log("登录失败（Token 失效或页面异常），结束本次运行")
                        # need_restart 保持 False → 外层 break
                else:
                    log("登录 OK！")
                    # ---- session 循环 ----
                    while need_restart is False:
                        if runtime_exceeded() or (hard_deadline and time.time() > hard_deadline):
                            log("达到最大运行时长，结束")
                            break

                        session += 1
                        log("")
                        log("=== Session #%d（浏览器实例 %d）==="
                            % (session, browser_restarts + 1))

                        try:
                            status = run_earn_session(sb, session, token, hard_deadline)
                        except BaseException as e:
                            dead = not driver_alive(sb)
                            log("Session 内异常: %s （浏览器存活: %s）"
                                % (str(e)[:120], "是" if not dead else "否"))
                            status = "dead" if dead else False

                        if status is None:          # 到达运行时长
                            break
                        if status == "dead":        # 浏览器死亡 → 重建
                            need_restart = True
                            restart_reason = "session 中浏览器死亡"
                            break
                        if status == "proxy_dead":  # 代理连不上 → 立刻切换（不等 2 次失败）
                            old = proxy
                            _next_proxy()
                            tf_fail_in_row = 0
                            need_restart = True
                            restart_reason = ("代理失效: %s -> %s"
                                              % (old or "直连", proxy or "直连"))
                            log("代理连接失败 → %s" % restart_reason)
                            break
                        if status is True:
                            tf_fail_in_row = 0
                        else:
                            tf_fail_in_row += 1
                            log("Session #%d 失败（连续失败 %d 次）"
                                % (session, tf_fail_in_row))

                            # 验证码级连续失败 → 也切换代理
                            if (len(proxy_cycle) > 1
                                    and tf_fail_in_row >= PROXY_FAIL_SWITCH):
                                old = proxy
                                _next_proxy()
                                tf_fail_in_row = 0
                                need_restart = True
                                restart_reason = ("代理切换: %s -> %s"
                                                  % (old or "直连", proxy or "直连"))
                                log("连续 %d 次失败 → %s" % (PROXY_FAIL_SWITCH, restart_reason))
                                break

                            wait = RETRY_DELAY + min(30, tf_fail_in_row * RETRY_DELAY)
                            log("%d 秒后重试…" % wait)
                            time.sleep(wait)

                        time.sleep(5)
        except BaseException as e:
            # 理论上不会走到（test=True 时 SB 吞异常），兜底
            need_restart = True
            restart_reason = "浏览器级异常: %s" % str(e)[:150]

        if not need_restart:
            break  # 正常运行结束（超时/登录失败/手动停止）

        browser_restarts += 1
        log("浏览器重建 %d/%d（%s）…" % (browser_restarts, BROWSER_RESTART_MAX, restart_reason))
        time.sleep(8)

    log("运行结束！")


if __name__ == "__main__":
    main()
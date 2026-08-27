#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FreezeHost AFK - 自动挂机赚币脚本（修复版 v2）
=================================================
相比原版的修复（逐条对应你日志里的三个问题）：

[问题1] Turnstile 永远失败（三个 session 全部 "Turnstile failed!"）
  * 根因A：原 wait_turnstile 每 5 秒调一次 uc_gui_click_captcha()。
    每次调用 = WebDriver 断开 + 鼠标点击 + 2.6 秒重连（UC.RECONNECT_TIME=2.4s）。
    也就是每 5 秒就把验证码点一遍、连接掐一遍——验证过程被反复打断，
    且这种机器人式狂点会触发 Cloudflare 升级为图片挑战，永远过不了。
    → 修复：先静置 25 秒等待托管型挑战自动通过；未通过才精准点击，
      每次点击间隔 >= 18 秒，每个 session 最多点 3 次。
  * 根因B：默认 frame="iframe" 取的是页面【第一个】iframe。
    /earn 页面上有 Funding Choices/广告等一堆 iframe，第一个未必是
    Turnstile，pyautogui 就点在了空白处。
    → 修复：用 frame='iframe[src*="challenges.cloudflare.com"]'
      精确指定 Turnstile 自己的 iframe（SeleniumBase 支持任意 CSS 选择器）。

[问题2] Error: The operation was canceled.
  * 根因：浏览器/CDP 连接在命令执行中途被掐断时抛出的错误
    （asyncio.CancelledError 在 Python 3.8+ 是 BaseException，
    原脚本里所有的 except: 都拦不住；或驱动连接被取消导致的 I/O 错误）。
    前两个 session 约 50 次断开/重连风暴后，浏览器在第 3 个 session 挂掉。
  * 原脚本对【任何】异常零恢复能力，一次崩溃整个进程退出、GitHub Actions 任务失败。
    → 修复：session 级捕获 + 死链检测；浏览器死了自动重建并重新登录；
      外层无限重试循环，连续失败指数退避。

[问题3] WARP IP 被 Cloudflare 标记
  * README 里 "WARP IP 是 Cloudflare 信任的" 已过时——WARP 出口 IP
    在免费宿主站上被 Challenge 得很凶，这是验证码过不去的最大外因。
    → 修复：PROXY_MODE=auto（默认）：连续 2 个 session 失败 → 自动切直连
      重建浏览器再试；直连也失败 → 切回 WARP，来回切换直到通过。

其他：* 等待期间轮询 + 到期前优雅退出
      * 截图保存路径兼容 Windows（tempfile）
      * 全部中文日志，方便你在 Actions 日志里排查
"""
import os
import time
import sys
import platform
import tempfile
import traceback

# ---------------------------------------------------------------------------
# 环境变量配置
# ---------------------------------------------------------------------------
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")                     # Discord Token（必填，逗号分隔支持多开）
WARP_PROXY = os.environ.get("WARP_PROXY", "socks5://127.0.0.1:40000")   # WARP 代理，置空禁用
MAX_RUNTIME = int(os.environ.get("MAX_RUNTIME", "0"))                   # 最大运行时长（分钟），0=无限
SESSION_DURATION = int(os.environ.get("SESSION_DURATION", "1200"))      # 每个 session 赏币时长（秒）
INSTANCE_ID = int(os.environ.get("INSTANCE_ID", "0"))                   # 实例编号
LOG_FILE = os.environ.get("LOG_FILE", "")                               # 日志文件（可选）
PROXY_MODE = os.environ.get("PROXY_MODE", "auto")                       # warp=只用WARP | direct=只用直连 | auto=失败自动切换
TRY_SOLVE = os.environ.get("TRY_SOLVE", "0") == "1"                     # 1=点击后额外尝试用 uc_gui_handle_captcha 解图片挑战

# Turnstile 行为参数（按需微调）
TURNSTILE_QUIET = int(os.environ.get("TURNSTILE_QUIET", "25"))          # 进页面先静置 N 秒（托管型自动通过）
CLICK_GAP = int(os.environ.get("CLICK_GAP", "18"))                      # 两次人工点击最小间隔（秒）
MAX_CLICKS = int(os.environ.get("MAX_CLICKS", "3"))                     # 每个 session 最多点击次数
CHALLENGE_TIMEOUT = int(os.environ.get("CHALLENGE_TIMEOUT", "150"))     # 单次验证码总超时（秒）
PROXY_FAIL_SWITCH = int(os.environ.get("PROXY_FAIL_SWITCH", "2"))       # 连续失败 N 个 session 切换代理
BROWSER_RESTART_MAX = int(os.environ.get("BROWSER_RESTART_MAX", "8"))   # 浏览器重建次数上限
RETRY_DELAY = int(os.environ.get("RETRY_DELAY", "10"))                  # session 失败后的基础等待（秒）

TURNSTILE_IFRAME = 'iframe[src*="challenges.cloudflare.com"]'           # Turnstile 专属 iframe 选择器

# Linux 服务器上需要虚拟显示器（GitHub Actions 也是如此）
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

# asyncio.CancelledError 在 Py3.8+ 是 BaseException，普通 except 拦不住，这里显式引入
try:
    from asyncio import CancelledError as _CancelledError
except ImportError:  # 理论不会发生，兜底
    _CancelledError = Exception

# 自定义异常：表示浏览器需要整体重建
class DriverDied(Exception):
    pass


global_start = time.time()


def log(msg):
    """带时间戳和实例编号的日志（全中文）"""
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
# Turnstile：取值 / 就绪检测 / 精准点击
# ---------------------------------------------------------------------------
def _turnstile_value(sb):
    """读取 cf-turnstile-response 的值（验证通过才会有值）"""
    try:
        v = sb.execute_script(
            "var t=document.querySelector('[name=cf-turnstile-response]');"
            "return t ? (t.value || '') : '';"
        ) or ""
        return str(v)
    except Exception:
        return ""


def _turnstile_present(sb):
    try:
        return bool(sb.execute_script(
            "return !!document.querySelector("
            "'iframe[src*=\"challenges.cloudflare.com\"], .cf-turnstile, "
            "[name=cf-turnstile-response], [data-callback=\"onCaptchaSuccess\"]');"
        ))
    except Exception:
        return False


def click_turnstile_once(sb):
    """精准点击 Turnstile 复选框（只点它自己的 iframe，绝不点页面第一个 iframe）"""
    try:
        if sb.is_element_present(TURNSTILE_IFRAME):
            # 指定 frame=选择器：SeleniumBase 会先取该 iframe 的位置，
            # 再进入 iframe 内部点复选框 span 的正中心
            ok = sb.uc_gui_click_captcha(frame=TURNSTILE_IFRAME)
        else:
            ok = sb.uc_gui_click_captcha()
        if ok:
            log("已点击 Turnstile 复选框")
        else:
            log("Turnstile 点击未生效（可能组件尚未渲染/已是自动模式）")
        return bool(ok)
    except BaseException as e:
        log("Turnstile 点击异常: %s" % str(e)[:120])
        return False


def wait_turnstile(sb, timeout=CHALLENGE_TIMEOUT):
    """
    阶段1：完全静置，等托管型挑战自动完成（不做任何操作）
    阶段2：精准点击（最多 MAX_CLICKS 次，每次间隔 CLICK_GAP 秒）
    """
    start = time.time()
    last_click = 0.0
    click_count = 0

    # ---- 阶段1：静置等待 ------------------------------------------------
    quiet_end = min(start + TURNSTILE_QUIET, start + timeout)
    while time.time() < quiet_end:
        v = _turnstile_value(sb)
        if v and len(v) > 20:
            log("Turnstile 自动通过！（静置方式）")
            return v
        time.sleep(2)

    # ---- 阶段2：必要时精准点击 -------------------------------------------
    log("静置 %ds 仍未通过，开始按需精准点击（最多 %d 次、间隔 %ds）..."
        % (TURNSTILE_QUIET, MAX_CLICKS, CLICK_GAP))
    while time.time() - start < timeout:
        v = _turnstile_value(sb)
        if v and len(v) > 20:
            log("Turnstile 验证通过！")
            return v
        if runtime_exceeded():
            return None
        if click_count < MAX_CLICKS and (time.time() - last_click) >= CLICK_GAP:
            if _turnstile_present(sb):
                click_count += 1
                last_click = time.time()
                click_turnstile_once(sb)
            else:
                time.sleep(3)
                continue
        else:
            time.sleep(3)
    return None


# ---------------------------------------------------------------------------
# 登录（Discord Token 注入 OAuth）
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
# 绕过广告拦截检测 + 点击 Start AFK
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
# 单个赚币 session
# ---------------------------------------------------------------------------
def run_earn_session(sb, session_num, token, hard_deadline):
    log("加载 /earn…")
    sb.uc_open_with_reconnect("https://free.freezehost.pro/earn", reconnect_time=6)
    time.sleep(15)

    url = sb.get_current_url()
    if not url.startswith("https://free.freezehost.pro"):
        log("会话过期，重新登录…")
        if not login_via_discord_token(sb, token):
            return False
        sb.uc_open_with_reconnect("https://free.freezehost.pro/earn", reconnect_time=6)
        time.sleep(15)

    log("等待 Turnstile（最多 %ds）…" % CHALLENGE_TIMEOUT)
    token_val = wait_turnstile(sb, timeout=CHALLENGE_TIMEOUT)
    if token_val is None:
        if runtime_exceeded():
            log("已达到最大运行时长")
            return None
        log("Turnstile 验证失败！")
        try:
            shot = os.path.join(tempfile.gettempdir(),
                                "fh_fail_%d_%d.png" % (INSTANCE_ID, session_num))
            sb.save_screenshot(shot)
            log("失败截图已保存: %s" % shot)
        except Exception:
            pass
        return False

    log("Turnstile 通过，继续…")

    if not click_start_afk(sb):
        log("警告：Start AFK 按钮点击失败，继续观察")

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
        except BaseException as e:
            log("会话检查异常（可能是浏览器问题）: %s" % str(e)[:100])
            raise
        time.sleep(30)

    log("Session #%d 完成 ✓" % session_num)
    return True


# ---------------------------------------------------------------------------
# 主循环：session + 浏览器重建 + 代理回退
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
    log("FreezeHost AFK 修复版 - 实例 #%d" % INSTANCE_ID)
    log("Token: %s...%s" % (token[:10], token[-5:]))
    log("代理: %s （模式: %s）" % (WARP_PROXY or "无", PROXY_MODE))
    log("=" * 56)

    proxy = None if PROXY_MODE == "direct" else WARP_PROXY
    hard_deadline = (global_start + MAX_RUNTIME * 60) if MAX_RUNTIME > 0 else 0

    browser_restarts = 0
    session = 0
    tf_fail_in_row = 0  # 连续 Turnstile 失败数（驱动崩溃不计入）

    while True:
        if runtime_exceeded():
            log("达到最大运行时长，结束")
            break
        if browser_restarts > BROWSER_RESTART_MAX:
            log("浏览器重建次数超过上限（%d），结束" % BROWSER_RESTART_MAX)
            break

        log("启动浏览器…（代理: %s，第 %d 个浏览器实例）"
            % (proxy or "直连", browser_restarts + 1))
        try:
            with SB(**build_options(proxy)) as sb:
                # 登录
                try:
                    login_ok = login_via_discord_token(sb, token)
                except BaseException as e:
                    log("登录阶段异常: %s" % str(e)[:120])
                    login_ok = False
                if not login_ok:
                    if not driver_alive(sb):
                        raise DriverDied("登录时浏览器死亡")
                    log("登录失败（Token 失效或页面异常），本轮结束")
                    break
                log("登录 OK！")

                # session 循环
                while True:
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
                        if dead:
                            raise DriverDied(str(e)[:120])
                        status = False  # 非致命错误，当一次失败重试

                    if status is None:  # 达到运行时长
                        break
                    if status is True:
                        tf_fail_in_row = 0
                    else:
                        tf_fail_in_row += 1
                        log("Session #%d 失败（连续失败 %d 次）" % (session, tf_fail_in_row))

                        # ---- 代理回退：连续失败自动切换 --------------------
                        if PROXY_MODE == "auto" and tf_fail_in_row >= PROXY_FAIL_SWITCH:
                            old = proxy
                            proxy = None if proxy else WARP_PROXY
                            tf_fail_in_row = 0
                            log("连续 %d 次验证码失败 → 代理从 [%s] 切换为 [%s]，重建浏览器"
                                % (PROXY_FAIL_SWITCH, old or "直连", proxy or "直连"))
                            raise DriverDied("代理切换: %s -> %s" % (old or "直连", proxy or "直连"))

                        wait = RETRY_DELAY + min(30, tf_fail_in_row * RETRY_DELAY)
                        log("%d 秒后重试…" % wait)
                        time.sleep(wait)

                    time.sleep(5)

        except DriverDied as e:
            browser_restarts += 1
            log("浏览器需要重建（%s）→ 第 %d/%d 次重建"
                % (e, browser_restarts, BROWSER_RESTART_MAX))
            time.sleep(8)
            continue
        except BaseException as e:
            # 兜底：整个 with 块外部的异常（浏览器级）
            browser_restarts += 1
            log("浏览器级异常: %s" % str(e)[:150])
            log("详细堆栈:\n%s" % "".join(
                traceback.format_exception(type(e), e, e.__traceback__))[-800:])
            log("→ 第 %d/%d 次重建" % (browser_restarts, BROWSER_RESTART_MAX))
            time.sleep(8)
            continue

        break  # 正常结束（超时或被主动停止）

    log("运行结束！")


if __name__ == "__main__":
    main()

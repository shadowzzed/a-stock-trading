"""盘中每分钟统一入口：先拉分钟 K → 再扫方向二信号 → 推送

合并原来两条独立 cron：
- 旧 pull_minute_bars.py（每分钟拉全市场 quotes 写 minute_bars）
- 旧 monitor.py main（每分钟扫 watchlist 生成买卖信号并推送）

合并理由：保证"先有数据再扫信号"的时序，避免 monitor 读到旧数据。
用法（cron 每分钟 09-14 工作日）：
  cd /Users/luoxin/src/a-stock-trading && python3 -m trading_agent.intraday.intraday_tick
"""
from __future__ import annotations

import contextlib
import io
import os
import subprocess
import sys
import time
from datetime import datetime


LOG_FILE = "/Users/luoxin/shared/trading/logs/intraday_tick.log"
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)


def _log(msg: str):
    """所有调试信息写日志文件，不输出到 stdout（避免被 HappyClaw 推送为飞书消息）。"""
    try:
        with open(LOG_FILE, "a") as f:
            f.write(f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


def _real_home() -> str:
    """HappyClaw script-runner 会把 HOME 改到工作区，需用 REAL_HOME 或 pwd 还原。"""
    rh = os.environ.get("REAL_HOME")
    if rh and os.path.isdir(rh):
        return rh
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir
    except Exception:
        return os.path.expanduser("~")


REAL_HOME = _real_home()


def _is_trading_minute() -> bool:
    """周一至周五，9:25 或 9:30-11:30 或 13:00-15:00 内才扫。"""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    h, m = now.hour, now.minute
    if h == 9 and m == 25:
        return True
    if h == 9 and m >= 30:
        return True
    if h == 10:
        return True
    if h == 11 and m <= 30:
        return True
    if h in (13, 14):
        return True
    if h == 15 and m == 0:
        return True
    return False


def run_pull_minute_bars():
    """调用 happyclaw 下的 pull_minute_bars.py（stdout 吞到日志，不推用户）。"""
    pull_script = "/Users/luoxin/src/happyclaw/data/groups/main/trading/pull_minute_bars.py"
    if not os.path.exists(pull_script):
        _log(f"pull script not found: {pull_script}")
        return False, "script not found"
    try:
        # 关键修复：用 sys.executable 而非 "python3"
        # HappyClaw script-runner 的 PATH 下 python3 可能解析到系统 Python 3.9，
        # 而 mootdx/pandas 装在 Python 3.14，会触发 ImportError
        r = subprocess.run(
            [sys.executable, pull_script],
            timeout=55,
            capture_output=True,
            text=True,
            check=False,
        )
        _log(f"pull stdout: {r.stdout.strip()[-200:]}")
        if r.returncode != 0:
            _log(f"pull stderr: {r.stderr.strip()[-500:]}")
            return False, r.stderr.strip()[:200]
        return True, ""
    except subprocess.TimeoutExpired:
        _log("pull_minute_bars 超时")
        return False, "timeout"
    except Exception as e:
        _log(f"pull_minute_bars 异常: {e}")
        return False, str(e)


def run_monitor_scan():
    """运行方向二监控扫一次，吞掉 stdout；新信号 monitor 自己通过飞书 API 推送。"""
    os.environ["HOME"] = "/Users/luoxin"
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            from trading_agent.intraday.monitor import main as monitor_main
            monitor_main()
        _log(f"monitor stdout: {buf.getvalue().strip()[-500:]}")
        return True, ""
    except Exception as e:
        import traceback
        _log(f"monitor 异常: {traceback.format_exc()[-1000:]}")
        return False, str(e)


def main():
    if not _is_trading_minute():
        return
    start = time.time()
    _log(f"tick start")

    errors = []
    pulled, pull_err = run_pull_minute_bars()
    if not pulled:
        errors.append(f"pull 失败: {pull_err}")

    scanned, mon_err = run_monitor_scan()
    if not scanned:
        errors.append(f"monitor 失败: {mon_err}")

    elapsed = time.time() - start
    _log(f"tick done in {elapsed:.1f}s errors={len(errors)}")

    # 只在失败时才输出到 stdout（HappyClaw 会推送给用户）
    if errors:
        print(f"⚠️ intraday_tick {datetime.now().strftime('%H:%M')} 异常: " + " | ".join(errors))


if __name__ == "__main__":
    main()

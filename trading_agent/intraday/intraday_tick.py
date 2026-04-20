"""盘中每分钟统一入口：先拉分钟 K → 再扫方向二信号 → 推送

合并原来两条独立 cron：
- 旧 pull_minute_bars.py（每分钟拉全市场 quotes 写 minute_bars）
- 旧 monitor.py main（每分钟扫 watchlist 生成买卖信号并推送）

合并理由：保证"先有数据再扫信号"的时序，避免 monitor 读到旧数据。
用法（cron 每分钟 09-14 工作日）：
  cd /Users/luoxin/src/a-stock-trading && python3 -m trading_agent.intraday.intraday_tick
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime


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
    """调用 happyclaw 下的 pull_minute_bars.py（主采集脚本）。"""
    # 硬编码绝对路径：HappyClaw script runner 下 REAL_HOME 被设成 workspace，
    # 导致 os.path.join 产生 .../main/src/happyclaw/... 嵌套路径
    pull_script = "/Users/luoxin/src/happyclaw/data/groups/main/trading/pull_minute_bars.py"
    if not os.path.exists(pull_script):
        print(f"[tick] pull script not found: {pull_script}")
        return False
    try:
        subprocess.run(
            ["python3", pull_script],
            timeout=55,  # 单次最多 55 秒，避免和下一分钟 tick 冲突
            check=False,
        )
        return True
    except subprocess.TimeoutExpired:
        print(f"[tick] pull_minute_bars 超时")
        return False
    except Exception as e:
        print(f"[tick] pull_minute_bars 失败: {e}")
        return False


def run_monitor_scan():
    """运行方向二监控扫一次。"""
    # 恢复真实 HOME，monitor.py 内部用 os.path.expanduser('~/shared/trading/...')
    # 硬编码：HappyClaw script runner 下 REAL_HOME 可能指向 workspace
    os.environ["HOME"] = "/Users/luoxin"
    try:
        from trading_agent.intraday.monitor import main as monitor_main
        monitor_main()
        return True
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"[tick] monitor 失败: {e}")
        return False


def main():
    if not _is_trading_minute():
        return
    start = time.time()
    print(f"[tick {datetime.now().strftime('%H:%M:%S')}] start")

    # 1. 先拉数据
    pulled = run_pull_minute_bars()
    elapsed_pull = time.time() - start
    print(f"[tick] pull done in {elapsed_pull:.1f}s (ok={pulled})")

    # 2. 再扫信号（pull 失败也扫，可能用到前一分钟数据）
    run_monitor_scan()
    print(f"[tick] total {time.time()-start:.1f}s")


if __name__ == "__main__":
    main()

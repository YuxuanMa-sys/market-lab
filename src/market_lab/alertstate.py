"""今天盘中已经推过哪些票、什么档位。记在 git 的 alerts-state 分支上，不碰工作区，也不碰 main。

云端任务每次都是全新副本，记不住推过什么。之前用当天分钟线猜"上次检查大概看到了什么"，按真实运行时刻回放 8 天：
应推 125 条只推了 91 条，FIS -24.6%、NOW -24.3%、GOOG★ -12.3% 全天没提醒。猜不准是必然的：云端实际运行比整点
晚 7~43 分钟，那个时刻的检查可能根本没跑过，也可能跑了但没推。所以改成直接记，而且推送成功之后才记，
某一小时没跑成或没推成，下一小时还会推。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import data

ROOT = Path(__file__).resolve().parents[2]
BRANCH = "alerts-state"
STATE_FILE = "sent.json"
PENDING = data.CACHE_DIR / "alerts-pending.json"
RANK = {None: 0, "接近低位": 1, "低位": 2, "深度低位": 3}
_ERRORS = (subprocess.SubprocessError, OSError, ValueError)


def _git(*args: str, stdin: str | None = None) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "Claude", "GIT_AUTHOR_EMAIL": "noreply@anthropic.com",
           "GIT_COMMITTER_NAME": "Claude", "GIT_COMMITTER_EMAIL": "noreply@anthropic.com"}
    return subprocess.run(["git", *args], cwd=ROOT, input=stdin, capture_output=True, text=True,
                          check=True, timeout=60, env=env).stdout.strip()


def _remote_head() -> str | None:
    out = _git("ls-remote", "--heads", "origin", BRANCH)
    return out.split()[0] if out else None


def _read(head: str | None) -> dict:
    if not head:
        return {}
    _git("fetch", "-q", "origin", BRANCH)
    return json.loads(_git("show", f"{head}:{STATE_FILE}"))


def load_sent(day: str) -> dict[str, str] | None:
    """这一天推过的 {票: 档位}。还没推过返回 {}，读不到（网络、权限）返回 None。"""
    try:
        rec = _read(_remote_head())
    except _ERRORS:
        return None
    return dict(rec.get("sent") or {}) if rec.get("date") == day else {}


def write_pending(day: str, sent: dict[str, str]) -> None:
    """这次要推的票先放在本地，等推送成功后 commit_pending 才算数。"""
    PENDING.parent.mkdir(parents=True, exist_ok=True)
    PENDING.write_text(json.dumps({"date": day, "sent": sent}, ensure_ascii=False), encoding="utf-8")


def _push(day: str, sent: dict[str, str], parent: str | None) -> None:
    payload = json.dumps({"date": day, "sent": sent}, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    blob = _git("hash-object", "-w", "--stdin", stdin=payload)
    tree = _git("mktree", stdin=f"100644 blob {blob}\t{STATE_FILE}\n")
    commit = _git("commit-tree", tree, "-m", f"盘中提醒记录 {day}", *(["-p", parent] if parent else []))
    _git("push", "-q", "origin", f"{commit}:refs/heads/{BRANCH}")


def commit_pending() -> int:
    """把 write_pending 留下的票并进当天记录并推到分支，返回记了几只。档位只升不降。

    读和写用同一个 head：别的运行抢先更新时，这次推送会被拒绝，重读后再试一次，不会盖掉对方的记录。
    """
    if not PENDING.exists():
        return 0
    rec = json.loads(PENDING.read_text(encoding="utf-8"))
    day, new = rec["date"], rec["sent"]
    count = len(new)
    for attempt in range(2 if new else 0):
        try:
            head = _remote_head()
            old = _read(head)
            if old.get("date", "") > day:      # 留下的是更早一天的：不能拿它盖掉今天的记录
                count = 0
                break
            sent = dict(old.get("sent") or {}) if old.get("date") == day else {}
            for t, tier in new.items():
                if RANK.get(tier, 0) > RANK.get(sent.get(t), 0):
                    sent[t] = tier
            _push(day, sent, head)
            break
        except subprocess.CalledProcessError as e:
            if attempt == 1:
                raise RuntimeError(f"git {e.cmd[1]} 失败: {(e.stderr or '').strip()}") from e
    PENDING.unlink()
    return count

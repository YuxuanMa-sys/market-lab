"""盘中提醒记录分支：用本地裸仓库跑真 git。"""
import subprocess

import pytest

from market_lab import alertstate


def _run(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    bare = tmp_path / "origin.git"
    _run(tmp_path, "init", "--bare", "-b", "main", str(bare))
    work = tmp_path / "work"
    _run(tmp_path, "clone", str(bare), str(work))
    _run(work, "config", "user.email", "t@example.com")
    _run(work, "config", "user.name", "t")
    (work / "a.txt").write_text("hello")
    _run(work, "add", "a.txt")
    _run(work, "commit", "-m", "init")
    _run(work, "push", "origin", "HEAD:main")
    monkeypatch.setattr(alertstate, "ROOT", work)
    monkeypatch.setattr(alertstate, "PENDING", tmp_path / "cache" / "pending.json")
    return work


def test_no_branch_means_nothing_sent(repo):
    assert alertstate.load_sent("2026-11-05") == {}


def test_unreadable_is_none_not_empty(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(alertstate, "ROOT", tmp_path / "does-not-exist")
    assert alertstate.load_sent("2026-11-05") is None


def test_commit_then_read_back_without_touching_main(repo):
    head = _run(repo, "rev-parse", "HEAD")
    alertstate.write_pending("2026-11-05", {"CVS": "低位"})
    assert alertstate.load_sent("2026-11-05") == {}                 # 推送成功之前不算推过
    assert alertstate.commit_pending() == 1
    assert alertstate.load_sent("2026-11-05") == {"CVS": "低位"}
    assert _run(repo, "rev-parse", "HEAD") == head and _run(repo, "status", "--short") == ""
    assert _run(repo, "ls-remote", "origin", "refs/heads/main").split()[0] == head
    assert alertstate.commit_pending() == 0                         # 同一批不重复记


def test_merge_keeps_higher_tier_and_new_day_starts_over(repo):
    alertstate.write_pending("2026-11-05", {"CVS": "深度低位", "NOW": "低位"})
    alertstate.commit_pending()
    alertstate.write_pending("2026-11-05", {"CVS": "低位", "NOW": "深度低位", "FIS": "低位"})
    assert alertstate.commit_pending() == 3
    assert alertstate.load_sent("2026-11-05") == {"CVS": "深度低位", "NOW": "深度低位", "FIS": "低位"}
    assert alertstate.load_sent("2026-11-06") == {}
    alertstate.write_pending("2026-11-06", {"FIS": "低位"})
    alertstate.commit_pending()
    assert alertstate.load_sent("2026-11-06") == {"FIS": "低位"}
    assert alertstate.load_sent("2026-11-05") == {}                 # 分支上只留最新一天，历史在 git log 里


def test_empty_pending_is_a_no_op(repo):
    alertstate.write_pending("2026-11-05", {})
    assert alertstate.commit_pending() == 0
    assert _run(repo, "ls-remote", "--heads", "origin", alertstate.BRANCH) == ""


def test_failed_push_keeps_pending_and_says_why(repo, monkeypatch):
    alertstate.write_pending("2026-11-05", {"CVS": "低位"})
    real_push = alertstate._push
    def boom(*a):
        raise subprocess.CalledProcessError(1, ["git", "push"], stderr="remote: 403 forbidden\n")
    monkeypatch.setattr(alertstate, "_push", boom)
    with pytest.raises(RuntimeError, match="git push 失败: remote: 403 forbidden"):
        alertstate.commit_pending()
    monkeypatch.setattr(alertstate, "_push", real_push)
    assert alertstate.commit_pending() == 1


def test_stale_pending_cannot_overwrite_a_later_day(repo):
    """回归：误跑 --mark-sent 时，昨天留下的记录会盖掉今天的，今天推过的票全部再推一遍。"""
    alertstate.write_pending("2026-11-05", {"B": "低位"})
    alertstate.commit_pending()
    alertstate.write_pending("2026-11-04", {"A": "低位"})
    assert alertstate.commit_pending() == 0
    assert alertstate.load_sent("2026-11-05") == {"B": "低位"} and not alertstate.PENDING.exists()


def test_racing_run_is_rejected_and_merged_not_overwritten(repo, monkeypatch):
    """回归：读记录和写提交各取一次 head，别的运行夹在中间推送时，它的记录会被盖掉。"""
    alertstate.write_pending("2026-11-05", {"BASE": "低位"})
    alertstate.commit_pending()
    alertstate.write_pending("2026-11-05", {"P2": "低位"})
    real_read, fired = alertstate._read, []
    pending = alertstate.PENDING

    def racing_read(head):
        out = real_read(head)
        if not fired:                                 # 这次读完之后，另一个运行抢先推送
            fired.append(1)
            alertstate.PENDING = pending.with_name("other.json")
            alertstate.write_pending("2026-11-05", {"P1": "低位"})
            monkeypatch.setattr(alertstate, "_read", real_read)
            alertstate.commit_pending()
            monkeypatch.setattr(alertstate, "_read", racing_read)
            alertstate.PENDING = pending
        return out

    monkeypatch.setattr(alertstate, "_read", racing_read)
    assert alertstate.commit_pending() == 1
    assert alertstate.load_sent("2026-11-05") == {"BASE": "低位", "P1": "低位", "P2": "低位"}

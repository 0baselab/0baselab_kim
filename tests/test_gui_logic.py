import os
from datetime import datetime

import pytest

from collector import gui_logic as L
from collector.collect import in_recommended_window, run_collect
from collector.secrets import save_keys
from collector.timeutil import KST

from .fake_market import FakeMarket
from .test_collect import KEYS, NOW, collect, install, kis_settings, reopen, start  # noqa: F401


def test_collect_mode():
    assert L.collect_mode("20261119", "20261119", "20261119") == ("daily", None, None)
    assert L.collect_mode("20261101", "20261119", "20261119") == ("range", "20261101", "20261119")


def test_recommended_window():
    d = lambda h, m: datetime(2026, 11, 19, h, m, tzinfo=KST)
    assert in_recommended_window(d(20, 30)) and in_recommended_window(d(22, 0))
    assert not in_recommended_window(d(20, 29)) and not in_recommended_window(d(22, 1))


def test_off_hours_mark_and_refetch(install):
    kis_settings(install)
    m = FakeMarket()
    collect(install, m, now=NOW.replace(hour=19))  # 권장 시간대 밖
    con = reopen(install)
    marked = con.execute("SELECT COUNT(*) FROM collect_log WHERE off_hours=1").fetchone()[0]
    assert marked > 0
    assert con.execute("SELECT off_hours FROM run_log").fetchone()[0] == 1
    m.krx_calls = 0
    m.investor_calls = 0
    # 표시된 데이터도 다시 받기
    a = start(install, m, NOW.replace(minute=45))
    try:
        res = run_collect(a, out=lambda *_: None, refetch_off_hours=True)
    finally:
        a.close()
    assert m.krx_calls > 0 and m.investor_calls > 0
    assert any("시간대 밖 표시 기록" in l for l in res.lines)
    # 권장 시간대에 다시 받은 기록은 표시가 지워짐
    assert con.execute("SELECT COUNT(*) FROM collect_log WHERE off_hours=1 AND source='KRX'").fetchone()[0] == 0


def test_kinds_selection(install):
    kis_settings(install)
    m = FakeMarket()
    a = start(install, m)
    try:
        run_collect(a, out=lambda *_: None, do_kis=False)
    finally:
        a.close()
    assert m.investor_calls == 0 and m.krx_calls > 0


def test_history(install):
    kis_settings(install)
    collect(install, FakeMarket())
    rows = L.run_history(reopen(install))
    assert rows[0]["target"] == "당일" and rows[0]["kinds"] == "KRX,KIS" and rows[0]["result"] == "완료"


def test_keys_and_version(tmp_path):
    assert L.missing_keys(tmp_path) == ["KRX_AUTH_KEY", "KIS_APP_KEY", "KIS_APP_SECRET", "DART_API_KEY"]
    save_keys(tmp_path, KEYS)
    assert L.missing_keys(tmp_path) == []
    assert L.version_changed(tmp_path) is False  # 첫 기록
    (tmp_path / "config" / "last_version").write_text("0.0.1")
    assert L.version_changed(tmp_path) is True
    assert L.version_changed(tmp_path) is False


def test_theme_import_export(tmp_path):
    src = tmp_path / "my.csv"
    src.write_text("theme,code,name,basis,added_date,note\nHBM,005930,a,b,2026-11-20,\n", encoding="utf-8-sig")
    td = tmp_path / "themes"
    L.import_theme(src, td, NOW)
    src.write_text("theme,code,name,basis,added_date,note\nAI,000660,a,b,2026-11-20,\n", encoding="utf-8-sig")
    L.import_theme(src, td, NOW.replace(second=1))
    assert len(list((td / "history").glob("*.csv"))) == 1
    assert "테마 1개, 1줄" in L.theme_summary(td)
    out = L.export_theme(td, tmp_path / "out.csv")
    assert "AI,000660" in out.read_text(encoding="utf-8-sig")


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="화면(DISPLAY) 없음")
def test_gui_smoke(install, monkeypatch):
    import tkinter as tk
    from collector import gui, paths
    monkeypatch.setattr(paths, "install_dir", lambda: install)
    monkeypatch.setattr(gui.paths, "install_dir", lambda: install)
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda *a, **k: None)
    root = tk.Tk()
    w = gui.MainWindow(root)
    for tab in (w.tab_collect, w.tab_brief, w.tab_theme, w.tab_settings):
        w.nb.select(tab)
        root.update()
    assert "테마 마스터" in w.t_info.cget("text") or "theme_master" in w.t_info.cget("text")
    w._log("  KRX 20/100 (20261118 trade_KOSPI)")
    assert int(w.progress["value"]) == 20
    w.s_scale.set("크게 (1.2배)")
    w._on_scale()
    assert w.scale == 1.2
    root.destroy()


@pytest.mark.skipif(not os.environ.get("DISPLAY"), reason="화면(DISPLAY) 없음")
def test_gui_collect_and_briefing_flow(install, monkeypatch):
    import time
    import tkinter as tk
    from collector import app as app_mod, gui, paths
    kis_settings(install)
    m = FakeMarket()
    real_start = app_mod.start
    monkeypatch.setattr(app_mod, "start", lambda **kw: real_start(install=install, now=lambda: NOW,
                                                                   http_factory=m.factory(), out=kw.get("out", print),
                                                                   prompt_keys=False))
    monkeypatch.setattr(gui.paths, "install_dir", lambda: install)
    monkeypatch.setattr(gui, "now_kst", lambda: NOW)
    monkeypatch.setattr(gui.messagebox, "askyesno", lambda *a, **k: True)
    popups = []
    monkeypatch.setattr(gui.MainWindow, "_result_popup", lambda self, t, body, folder: popups.append((t, body)))
    root = tk.Tk()
    w = gui.MainWindow(root)

    def wait():
        t0 = time.time()
        root.update()
        while w.busy and time.time() - t0 < 60:
            root.update()
            time.sleep(0.02)
        root.update()

    w._on_collect()
    wait()
    assert popups and popups[-1][0] == "수집 결과" and "KIS 수급" in popups[-1][1]
    w.b_date.set("20261119")
    w._on_briefing()
    wait()
    assert popups[-1][0] == "브리핑 결과" and "대상 종목 4개" in popups[-1][1]
    root.destroy()

from pathlib import Path

from collector import paths
from collector.http import RateLimiter
from collector.secrets import ensure_keys, keys_path, mask, redact, register_sensitive
from collector.settings import load_settings, settings_path
from collector.verify.common import num


def test_data_dir_override_and_linux_fallback(tmp_path, monkeypatch):
    assert paths.data_dir({"DATA_DIR": str(tmp_path / "d")}) == tmp_path / "d"
    # Path.home()은 Linux·macOS에서 HOME, Windows에서 USERPROFILE을 본다.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert paths.data_dir({}, platform="linux") == tmp_path / "Desktop" / "주식데이터수집"


def test_onedrive_detect():
    assert paths.is_onedrive(Path("C:/Users/a/OneDrive/바탕 화면"))
    assert not paths.is_onedrive(Path("C:/data"))


def test_settings_created_and_new_keys_appended_without_overwrite(tmp_path):
    s = load_settings(tmp_path)
    assert s["KIS_CALLS_PER_SEC"] == "10"
    assert s["KRX_INDEX_KOSPI_URL"].endswith("/idx/kospi_dd_trd")
    p = settings_path(tmp_path)
    p.write_text("KIS_CALLS_PER_SEC=7\n", encoding="utf-8")
    s = load_settings(tmp_path)
    assert s["KIS_CALLS_PER_SEC"] == "7"  # 사용자 값 유지
    assert "DART_DAILY_LIMIT" in s  # 빠진 기본값만 덧붙임
    assert p.read_text(encoding="utf-8").count("KIS_CALLS_PER_SEC=") == 1


def test_keys_prompt_save_and_mask(tmp_path):
    answers = iter(["KRXKEY123456", "KISAPPKEY0001", "KISSECRET00001", "DARTKEY000001"])
    shown = []
    keys = ensure_keys(tmp_path, ask=lambda _: next(answers), out=shown.append)
    assert keys_path(tmp_path).exists()
    assert keys["KIS_APP_KEY"] == "KISAPPKEY0001"
    assert all("KISAPPKEY0001" not in s for s in shown)
    assert mask("KISAPPKEY0001") == "KISA****"
    # 두 번째 실행은 묻지 않음
    ensure_keys(tmp_path, ask=lambda _: (_ for _ in ()).throw(AssertionError("asked")), out=shown.append)


def test_redact():
    register_sensitive("SECRETVALUE123")
    assert redact("x SECRETVALUE123 y") == "x **** y"


def test_num_never_zero_fills():
    assert num("1,234") == 1234
    assert num("-") is None
    assert num("") is None
    assert num(None) is None
    assert num("-5") == -5


def test_rate_limiter_spaces_calls_evenly():
    t = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        t[0] += s

    rl = RateLimiter(10, clock=lambda: t[0], sleep=sleep)
    for _ in range(3):
        rl.wait()
    assert len(slept) == 2 and all(abs(x - 0.1) < 1e-9 for x in slept)
    rl.set_rate(5)
    rl.wait()
    assert abs(slept[-1] - 0.2) < 1e-9


def test_settings_migrates_old_default_only(tmp_path):
    p = settings_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text("KIS_CALLS_PER_SEC=15\n", encoding="utf-8")
    assert load_settings(tmp_path)["KIS_CALLS_PER_SEC"] == "10"
    p.write_text("KIS_CALLS_PER_SEC=7\nKRX_RETENTION_DAYS=250\n", encoding="utf-8")
    s = load_settings(tmp_path)
    assert s["KIS_CALLS_PER_SEC"] == "7" and s["KRX_RETENTION_DAYS"] == "260"


def test_investor_window():
    from datetime import datetime
    from collector.kis import investor_window_ok
    from collector.timeutil import KST
    d = lambda h, m: datetime(2026, 11, 18, h, m, tzinfo=KST)
    assert not investor_window_ok(d(0, 0))
    assert not investor_window_ok(d(15, 39))
    assert investor_window_ok(d(15, 40))
    assert investor_window_ok(d(20, 30))
    assert investor_window_ok(d(23, 59))

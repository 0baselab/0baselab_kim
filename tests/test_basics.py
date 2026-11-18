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
    assert s["KIS_CALLS_PER_SEC"] == "15"
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


def test_rate_limiter_waits():
    t = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        t[0] += s

    rl = RateLimiter(3, clock=lambda: t[0], sleep=sleep)
    for _ in range(3):
        rl.wait()
    assert not slept
    rl.wait()
    assert slept and abs(slept[0] - 1.0) < 1e-9

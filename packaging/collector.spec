# PyInstaller 설정: exe 2개가 한 폴더(_internal 공유)에 들어간다.
#   주식데이터수집기.exe       창 전용 GUI (콘솔 창 없음, 더블클릭용)
#   주식데이터수집기-명령.exe  명령줄용 (daily, briefing 등. 인자 없이 실행하면 터미널 메뉴)
# 실행: python -m PyInstaller --noconfirm --clean packaging/collector.spec
from PyInstaller.utils.hooks import collect_data_files

ROOT = SPECPATH + "/.."
datas = collect_data_files("customtkinter") + collect_data_files("tzdata") + [
    (ROOT + "/collector/assets", "collector/assets"),
]

gui_a = Analysis([ROOT + "/gui_main.py"], pathex=[ROOT], datas=datas, hiddenimports=["tkinter"])
cli_a = Analysis([ROOT + "/run_collector.py"], pathex=[ROOT], datas=datas, hiddenimports=["tkinter"])

gui_pyz = PYZ(gui_a.pure)
cli_pyz = PYZ(cli_a.pure)

gui_exe = EXE(gui_pyz, gui_a.scripts, [], exclude_binaries=True, name="주식데이터수집기", console=False)
cli_exe = EXE(cli_pyz, cli_a.scripts, [], exclude_binaries=True, name="주식데이터수집기-명령", console=True)

coll = COLLECT(gui_exe, gui_a.binaries, gui_a.datas, cli_exe, cli_a.binaries, cli_a.datas, name="주식데이터수집기")

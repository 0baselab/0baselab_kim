# 토스측정 PyInstaller 설정 (명령줄 exe 1개, --onedir)
# 실행: python -m PyInstaller --noconfirm --clean packaging/toss.spec
from PyInstaller.utils.hooks import collect_data_files

ROOT = SPECPATH + "/.."
a = Analysis([ROOT + "/run_toss.py"], pathex=[ROOT], datas=collect_data_files("tzdata"),
             excludes=["tkinter", "customtkinter", "openpyxl"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="토스측정", console=True)
coll = COLLECT(exe, a.binaries, a.datas, name="토스측정")

# 단타분봉수집기 PyInstaller 설정 (명령줄 exe 1개, --onedir)
# 실행: python -m PyInstaller --noconfirm --clean packaging/danta.spec
from PyInstaller.utils.hooks import collect_data_files

ROOT = SPECPATH + "/.."
a = Analysis([ROOT + "/run_danta.py"], pathex=[ROOT], datas=collect_data_files("tzdata"),
             excludes=["tkinter", "customtkinter", "openpyxl"])
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="단타분봉수집기", console=True)
coll = COLLECT(exe, a.binaries, a.datas, name="단타분봉수집기")

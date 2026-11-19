"""창 전용 exe(주식데이터수집기.exe) 진입점. 콘솔 창 없이 GUI만 띄운다."""
import sys

from collector.gui import run_gui

if __name__ == "__main__":
    sys.exit(run_gui())

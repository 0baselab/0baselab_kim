"""GUI (UI/UX 결정사항 2026-11-19, CustomTkinter).

화면 구성: 왼쪽 네이비 내비게이션 + 오른쪽 흰 작업 영역. 페이지 4개 [수집] [브리핑 데이터 가공] [테마 관리] [설정].
[수집] 페이지 맨 위의 '저녁 시간 띠'가 이 화면의 기억에 남는 요소다: 15시~24시를 가로로 놓고,
KIS 조회 가능 시간(15:40~23:59)과 권장 수집 시간(20:30~22:00), 지금 시각을 한 줄에 보여준다.
나머지는 조용하게 둔다.

작업은 별도 스레드에서 돌리고, 화면 갱신은 큐를 통해 메인 스레드에서만 한다.
DB 연결과 잠금 파일은 작업 스레드 안에서 열고 닫는다.
"""
from __future__ import annotations

import logging
import queue
import re
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from . import gui_logic as L
from . import paths
from .secrets import KEY_NAMES, load_keys, mask, register_sensitive, save_keys
from .settings import load_settings, set_setting
from .timeutil import now_kst
from .version import __version__

# 색 (결정사항 고정값 + 같은 계열의 보조 톤)
NAVY = "#0B1F3A"
NAVY_2 = "#14305A"      # 내비게이션 선택 배경
COBALT = "#0047FF"
COBALT_DARK = "#0036C4"
GRAY = "#8A94A6"
WHITE = "#FFFFFF"
SURFACE = "#F3F5F9"     # 입력·로그 바탕 (차가운 회백색)
LINE = "#DCE1EA"        # 구분선
RED = "#D92D20"
YELLOW = "#F5B800"
YELLOW_BG = "#FFF6D6"
RED_BG = "#FDECEA"

SCALES = [("1.0", "보통"), ("1.2", "크게"), ("1.4", "아주 크게")]
PROGRESS_RE = re.compile(r"(\d+)/(\d+)")
FONT = "Pretendard"  # SIL OFL 1.1, collector/assets/fonts에 동봉 (PC에 설치하지 않고 이 프로그램 안에서만 불러옴)
FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"


def load_fonts() -> bool:
    """동봉한 Pretendard를 불러온다. 실패하면 Windows 기본 글꼴로 대신 그린다."""
    global FONT
    ok = all(ctk.FontManager.load_font(str(FONT_DIR / f)) for f in ("Pretendard-Regular.ttf", "Pretendard-Bold.ttf"))
    if not ok:
        FONT = "Malgun Gothic" if sys.platform == "win32" else "TkDefaultFont"
    return ok

PAGES = [("collect", "수집"), ("brief", "브리핑 데이터 가공"), ("theme", "테마 관리"), ("backtest", "백테스트"),
         ("settings", "설정")]

# 저녁 시간 띠 (분 단위)
BAND_START, BAND_END = 15 * 60, 24 * 60
KIS_START, KIS_END = 15 * 60 + 40, 23 * 60 + 59
REC_START, REC_END = 20 * 60 + 30, 22 * 60


def F(size=13, weight="normal"):
    return ctk.CTkFont(family=FONT, size=size, weight=weight)


class QueueLogHandler(logging.Handler):
    def __init__(self, q):
        super().__init__(logging.INFO)
        self.q = q

    def emit(self, record):
        try:
            self.q.put(("log", self.format(record)))
        except Exception:
            pass


class EveningBand(tk.Canvas):
    """15시~24시 시간 띠. KIS 조회 가능 / 권장 수집 / 지금 시각."""

    def __init__(self, master, **kw):
        super().__init__(master, height=66, bg=WHITE, highlightthickness=0, **kw)
        self.bind("<Configure>", lambda e: self.draw())

    def _x(self, minute, w, pad=24):
        return pad + (w - 2 * pad) * (minute - BAND_START) / (BAND_END - BAND_START)

    def draw(self, now=None):
        now = now or now_kst()
        self.delete("all")
        w = max(self.winfo_width(), 200)
        y0, y1 = 26, 40
        self.create_rectangle(self._x(BAND_START, w), y0, self._x(BAND_END, w), y1, fill=SURFACE, width=0)
        self.create_rectangle(self._x(KIS_START, w), y0, self._x(KIS_END, w), y1, fill=LINE, width=0)
        self.create_rectangle(self._x(REC_START, w), y0 - 4, self._x(REC_END, w), y1 + 4, fill=COBALT, width=0)
        for h in range(15, 25, 3):
            x = self._x(h * 60, w)
            self.create_text(x, y1 + 16, text=f"{h}:00" if h < 24 else "24:00", fill=GRAY, font=(FONT, 9))
        self.create_text(self._x(REC_START, w), 12, text="20:30", fill=COBALT, font=(FONT, 9, "bold"), anchor="w")
        self.create_text(self._x(REC_END, w), 12, text="22:00", fill=COBALT, font=(FONT, 9, "bold"), anchor="e")
        self.create_text(self._x(KIS_START, w), 12, text="KIS 조회 가능 15:40부터", fill=GRAY, font=(FONT, 9), anchor="w")
        m = now.hour * 60 + now.minute
        if BAND_START <= m <= BAND_END:
            x = self._x(m, w)
            self.create_line(x, y0 - 10, x, y1 + 10, fill=NAVY, width=2)
            self.create_oval(x - 4, y0 - 14, x + 4, y0 - 6, fill=NAVY, outline="")


class MainWindow:
    def __init__(self, root: ctk.CTk):
        self.root = root
        self.install = paths.install_dir()
        self.settings = load_settings(self.install)
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.last_log_path: Path | None = None
        self.scale = float(self.settings.get("GUI_FONT_SCALE") or 1.0)
        ctk.set_appearance_mode("light")
        ctk.set_widget_scaling(self.scale)
        root.title(f"주식데이터수집기 {__version__}")
        root.geometry("1040x720")
        root.minsize(900, 600)
        root.configure(fg_color=WHITE)
        root.grid_columnconfigure(1, weight=1)
        root.grid_rowconfigure(0, weight=1)
        self._action_buttons: list[ctk.CTkButton] = []
        self._build_nav()
        self.content = ctk.CTkFrame(root, fg_color=WHITE, corner_radius=0)
        self.content.grid(row=0, column=1, sticky="nsew")
        self.content.grid_columnconfigure(0, weight=1)
        self.content.grid_rowconfigure(0, weight=1)
        self.pages = {key: ctk.CTkFrame(self.content, fg_color=WHITE, corner_radius=0) for key, _ in PAGES}
        for p in self.pages.values():
            p.grid(row=0, column=0, sticky="nsew", padx=32, pady=28)
        self.tab_collect, self.tab_brief = self.pages["collect"], self.pages["brief"]
        self.tab_theme, self.tab_settings = self.pages["theme"], self.pages["settings"]
        self._build_collect()
        self._build_briefing()
        self._build_theme()
        self._build_settings()
        self.tab_backtest = self.pages["backtest"]
        self._build_backtest()
        self.show("collect")
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._poll)
        root.after(300, self._startup_checks)
        root.after(30_000, self._tick)

    # ---------------- 틀 ----------------
    def _build_nav(self):
        nav = ctk.CTkFrame(self.root, fg_color=NAVY, corner_radius=0, width=220)
        nav.grid(row=0, column=0, sticky="nsw")
        nav.grid_propagate(False)
        ctk.CTkLabel(nav, text="주식데이터수집기", text_color=WHITE, font=F(17, "bold"), anchor="w").pack(
            fill="x", padx=22, pady=(26, 2))
        ctk.CTkLabel(nav, text=f"버전 {__version__}", text_color=GRAY, font=F(11), anchor="w").pack(fill="x", padx=22)
        ctk.CTkFrame(nav, fg_color=NAVY_2, height=1).pack(fill="x", padx=22, pady=(20, 12))
        self.nav_buttons = {}
        for key, title in PAGES:
            b = ctk.CTkButton(nav, text=title, anchor="w", height=40, corner_radius=8, font=F(14),
                              fg_color="transparent", hover_color=NAVY_2, text_color=WHITE,
                              command=lambda k=key: self.show(k))
            b.pack(fill="x", padx=12, pady=2)
            self.nav_buttons[key] = b
        self.nav_note = ctk.CTkLabel(nav, text="", text_color=GRAY, font=F(11), anchor="w", justify="left",
                                     wraplength=180)
        self.nav_note.pack(side="bottom", fill="x", padx=22, pady=20)

    def show(self, key):
        self.pages[key].tkraise()
        for k, b in self.nav_buttons.items():
            on = k == key
            b.configure(fg_color=NAVY_2 if on else "transparent", font=F(14, "bold" if on else "normal"),
                        border_width=0)
        self.current = key
        if key == "theme":
            self._refresh_theme_info()

    def _title(self, parent, text, sub=None):
        ctk.CTkLabel(parent, text=text, font=F(22, "bold"), text_color=NAVY, anchor="w").pack(fill="x")
        if sub:
            ctk.CTkLabel(parent, text=sub, font=F(13), text_color=GRAY, anchor="w", justify="left",
                         wraplength=720).pack(fill="x", pady=(4, 0))

    def _section(self, parent, text):
        ctk.CTkLabel(parent, text=text, font=F(13, "bold"), text_color=NAVY, anchor="w").pack(fill="x", pady=(22, 8))

    def _primary(self, parent, text, cmd, **kw):
        b = ctk.CTkButton(parent, text=text, command=cmd, fg_color=COBALT, hover_color=COBALT_DARK,
                          text_color=WHITE, text_color_disabled="#D6DEF5", corner_radius=8, height=40,
                          font=F(14, "bold"), **kw)
        self._action_buttons.append(b)
        return b

    def _secondary(self, parent, text, cmd, **kw):
        return ctk.CTkButton(parent, text=text, command=cmd, fg_color=WHITE, hover_color=SURFACE,
                             text_color=NAVY, border_color=LINE, border_width=1, corner_radius=8, height=40,
                             font=F(13), **kw)

    def _entry(self, parent, var, width=120, show=None):
        return ctk.CTkEntry(parent, textvariable=var, width=width, height=36, corner_radius=8, fg_color=SURFACE,
                            border_color=LINE, text_color=NAVY, font=F(14), show=show)

    def _check(self, parent, text, var):
        return ctk.CTkCheckBox(parent, text=text, variable=var, font=F(13), text_color=NAVY, fg_color=COBALT,
                               hover_color=COBALT_DARK, border_color=GRAY, corner_radius=4, checkbox_width=20,
                               checkbox_height=20)

    def _notice(self, parent, kind="warn"):
        """경고(노랑)·오류(빨강) 띠. 평소엔 숨겨 둔다."""
        bg, edge = (YELLOW_BG, YELLOW) if kind == "warn" else (RED_BG, RED)
        box = ctk.CTkFrame(parent, fg_color=bg, corner_radius=8, border_width=0, height=40)
        bar = ctk.CTkFrame(box, fg_color=edge, width=4, height=1, corner_radius=2)
        bar.pack(side="left", fill="y", padx=(0, 10), pady=6)
        lbl = ctk.CTkLabel(box, text="", font=F(13), text_color=NAVY, anchor="w", justify="left", wraplength=640)
        lbl.pack(side="left", fill="x", expand=True, pady=8, padx=(0, 12))
        box.label = lbl
        return box

    # ---------------- 작업 실행 공통 ----------------
    def _run_worker(self, fn, done):
        if self.busy:
            messagebox.showinfo("작업 중", "다른 작업이 진행 중입니다. 끝난 뒤 다시 눌러 주세요.")
            return
        self.busy = True
        self._set_buttons("disabled")

        def target():
            try:
                result = fn(self.q.put)
                self.q.put(("done", done, result, None))
            except Exception as exc:
                logging.getLogger("collector").exception("작업 오류")
                self.q.put(("done", done, None, exc))

        threading.Thread(target=target, daemon=True).start()

    def _start_app(self, put):
        from .app import start
        app = start(prompt_keys=False, out=lambda s: put(("log", s)))
        h = QueueLogHandler(self.q)
        h.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        logging.getLogger("collector").addHandler(h)
        self.last_log_path = app.log_path
        return app

    def _ask_from_worker(self, question: str) -> bool:
        ev, holder = threading.Event(), {}
        self.q.put(("ask", question, ev, holder))
        ev.wait()
        return holder.get("answer", False)

    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                if msg[0] == "log":
                    self._log(msg[1])
                elif msg[0] == "bt":
                    self.bt_progress.set(msg[2])
                    self.bt_status.configure(text=msg[1], text_color=NAVY)
                elif msg[0] == "ask":
                    _, question, ev, holder = msg
                    holder["answer"] = messagebox.askyesno("확인", question)
                    ev.set()
                elif msg[0] == "done":
                    _, done, result, exc = msg
                    self.busy = False
                    self._set_buttons("normal")
                    if exc is not None:
                        self._finish_progress(error=True)
                        messagebox.showerror("작업을 끝내지 못했습니다", f"{type(exc).__name__}: {exc}\n\n"
                                             "[상세 로그 복사]로 로그를 복사해 알려 주세요.")
                    else:
                        done(result)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _set_buttons(self, state):
        for b in self._action_buttons:
            b.configure(state=state)

    def _on_close(self):
        if self.busy and not messagebox.askyesno(
                "종료", "작업이 진행 중입니다. 지금 끄면 진행 중인 단위만 버리고, 다음 실행 때 이어서 받습니다. 종료할까요?"):
            return
        self.root.destroy()

    def _startup_checks(self):
        """키가 없어도 막지 않는다(백테스트만 쓰는 PC). 왼쪽 메뉴 아래에 상태만 알린다."""
        changed = L.version_changed(self.install)
        miss = L.missing_keys(self.install)
        if changed:
            self._log(f"업데이트 확인: 버전 {__version__}, " + ("API 키 4개 모두 있음" if not miss else
                      "없는 키: " + ", ".join(miss) + " (수집하려면 [설정]에서 입력)"))
        self._refresh_nav_note()

    def _refresh_nav_note(self):
        miss = L.missing_keys(self.install)
        self.nav_note.configure(text=("API 키 없음: " + ", ".join(miss)) if miss else "API 키 4개 저장됨",
                                text_color=YELLOW if miss else GRAY)

    def _tick(self):
        self.band.draw()
        self._update_window_hint()
        self.root.after(30_000, self._tick)

    # ---------------- [수집] ----------------
    def _build_collect(self):
        t = self.tab_collect
        self._title(t, "수집", "매일은 장이 열린 날 저녁, 아래 [수집 실행]만 누르면 됩니다.")
        band_wrap = ctk.CTkFrame(t, fg_color=WHITE)
        band_wrap.pack(fill="x", pady=(18, 0))
        self.band = EveningBand(band_wrap)
        self.band.pack(fill="x")
        self.window_hint = ctk.CTkLabel(t, text="", font=F(13), anchor="w", text_color=NAVY)
        self.window_hint.pack(fill="x", pady=(2, 0))

        form = ctk.CTkFrame(t, fg_color=WHITE)
        form.pack(fill="x", pady=(18, 0))
        form.grid_columnconfigure(1, weight=1)
        today = now_kst().strftime("%Y%m%d")
        self.c_start, self.c_end = tk.StringVar(value=today), tk.StringVar(value=today)
        ctk.CTkLabel(form, text="기간", font=F(13, "bold"), text_color=NAVY, width=64, anchor="w").grid(row=0, column=0, sticky="w")
        r0 = ctk.CTkFrame(form, fg_color=WHITE)
        r0.grid(row=0, column=1, sticky="w")
        self._entry(r0, self.c_start).pack(side="left")
        ctk.CTkLabel(r0, text="부터", font=F(13), text_color=GRAY).pack(side="left", padx=8)
        self._entry(r0, self.c_end).pack(side="left")
        ctk.CTkLabel(r0, text="까지", font=F(13), text_color=GRAY).pack(side="left", padx=8)
        ctk.CTkLabel(form, text="오늘 하루로 두면 빠진 날도 함께 채웁니다. 날짜는 20261119처럼 숫자 8자리.",
                     font=F(12), text_color=GRAY, anchor="w").grid(row=1, column=1, sticky="w", pady=(4, 0))
        self.c_kis, self.c_krx, self.c_dart = tk.BooleanVar(value=True), tk.BooleanVar(value=True), tk.BooleanVar(value=False)
        self.c_refetch = tk.BooleanVar(value=False)
        ctk.CTkLabel(form, text="데이터", font=F(13, "bold"), text_color=NAVY, width=64, anchor="w").grid(
            row=2, column=0, sticky="w", pady=(16, 0))
        r2 = ctk.CTkFrame(form, fg_color=WHITE)
        r2.grid(row=2, column=1, sticky="w", pady=(16, 0))
        for text, var in (("KIS 수급·당일 시세", self.c_kis), ("KRX 확정 시세", self.c_krx), ("DART 재무·공시", self.c_dart)):
            self._check(r2, text, var).pack(side="left", padx=(0, 22))
        self._check(form, "'시간대 밖' 표시된 데이터도 다시 받기", self.c_refetch).grid(
            row=3, column=1, sticky="w", pady=(12, 0))

        run = ctk.CTkFrame(t, fg_color=WHITE)
        run.pack(fill="x", pady=(22, 0))
        self.btn_collect = self._primary(run, "수집 실행", self._on_collect, width=180)
        self.btn_collect.pack(side="left")
        self.c_status = ctk.CTkLabel(run, text="", font=F(13), text_color=GRAY, anchor="w")
        self.c_status.pack(side="left", padx=16, fill="x", expand=True)
        self.progress = ctk.CTkProgressBar(t, height=8, corner_radius=4, fg_color=SURFACE, progress_color=COBALT)
        self.progress.set(0)
        self.progress.pack(fill="x", pady=(16, 0))
        self.c_notice = self._notice(t, "err")

        head = ctk.CTkFrame(t, fg_color=WHITE)
        head.pack(fill="x", pady=(18, 6))
        ctk.CTkLabel(head, text="진행 기록", font=F(13, "bold"), text_color=NAVY).pack(side="left")
        self._secondary(head, "상세 로그 복사", self._copy_log, width=130).pack(side="right")
        self.logbox = ctk.CTkTextbox(t, fg_color=SURFACE, text_color=NAVY, corner_radius=8, border_width=0,
                                     font=F(12), wrap="word")
        self.logbox.pack(fill="both", expand=True)
        self.logbox.configure(state="disabled")
        self._update_window_hint()

    def _update_window_hint(self):
        from .collect import in_recommended_window
        now = now_kst()
        m = now.hour * 60 + now.minute
        hm = now.strftime("%H:%M")
        if in_recommended_window(now):
            text, color = f"지금 {hm}. 권장 수집 시간입니다.", COBALT
        elif KIS_START <= m <= KIS_END:
            text, color = f"지금 {hm}. 수집은 되지만 권장 시간(20:30~22:00)이 아니어서 기록에 '시간대 밖' 표시가 남습니다.", "#8A6100"
        else:
            text, color = f"지금 {hm}. KIS 수급은 15:40부터 조회됩니다. KRX·DART만 받으려면 KIS를 끄고 실행하세요.", "#8A6100"
        self.window_hint.configure(text=text, text_color=color)

    def _log(self, text):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", text.rstrip() + "\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")
        m = PROGRESS_RE.search(text)
        if m and any(k in text for k in ("KRX", "KIS", "DART")):
            a, b = int(m.group(1)), int(m.group(2))
            if self.progress.cget("mode") != "determinate":
                self.progress.stop()
                self.progress.configure(mode="determinate")
            self.progress.set(a / max(b, 1))
            self.c_status.configure(text=text.strip(), text_color=NAVY)

    def _finish_progress(self, error=False):
        self.progress.stop()
        self.progress.configure(mode="determinate", progress_color=RED if error else COBALT)
        self.progress.set(1)

    def _copy_log(self):
        text = ""
        if self.last_log_path and self.last_log_path.exists():
            text = self.last_log_path.read_text(encoding="utf-8", errors="replace")
        if not text:
            text = self.logbox.get("1.0", "end")
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.c_status.configure(text="상세 로그를 클립보드에 복사했습니다.", text_color=NAVY)

    def _on_collect(self):
        from .collect import in_recommended_window
        self.c_notice.pack_forget()
        start, end = self.c_start.get().strip(), self.c_end.get().strip()
        if not (re.fullmatch(r"\d{8}", start) and re.fullmatch(r"\d{8}", end)) or start > end:
            self._show_notice(self.c_notice, "기간을 확인하세요. 날짜는 숫자 8자리이고, 시작일이 종료일보다 늦으면 안 됩니다.")
            return
        if not (self.c_kis.get() or self.c_krx.get() or self.c_dart.get()):
            self._show_notice(self.c_notice, "받을 데이터를 하나 이상 고르세요.")
            return
        if not in_recommended_window(now_kst()) and (self.c_kis.get() or self.c_krx.get()):
            if not messagebox.askyesno(
                    "권장 시간대 밖", "지금은 권장 수집 시간(장 열린 날 20:30~22:00)이 아닙니다.\n"
                    "이 시간에 받은 데이터는 권장 시간대에 받는 데이터와 다를 수 있습니다.\n"
                    "이번 기록에는 '시간대 밖' 표시가 남고, 나중에 다시 받을 수 있습니다.\n\n계속할까요?",
                    icon="warning"):
                return
        mode, s, e = L.collect_mode(start, end, now_kst().strftime("%Y%m%d"))
        opts = dict(kis=self.c_kis.get(), krx=self.c_krx.get(), dart=self.c_dart.get(), refetch=self.c_refetch.get())
        self.logbox.configure(state="normal")
        self.logbox.delete("1.0", "end")
        self.logbox.configure(state="disabled")
        self.progress.configure(mode="indeterminate", progress_color=COBALT)
        self.progress.start()
        self.c_status.configure(text="수집하는 중", text_color=NAVY)

        def work(put):
            from .collect import run_collect
            app = self._start_app(put)
            out = {"lines": [], "failures": [], "root": app.root, "stopped": None}
            try:
                if opts["kis"] or opts["krx"]:
                    confirm = lambda q: True if "애프터마켓" in q else self._ask_from_worker(q)
                    res = run_collect(app, mode, s, e, confirm=confirm, out=lambda x: put(("log", x)),
                                      do_krx=opts["krx"], do_kis=opts["kis"], refetch_off_hours=opts["refetch"])
                    out["lines"] += res.lines
                    out["failures"] += [l.strip()[2:] for l in res.lines if l.startswith("  - ")]
                    out["stopped"] = res.stopped
                if opts["dart"]:
                    out["lines"] += self._run_dart(app, put, out)
            finally:
                app.close()
            return out

        self._run_worker(work, self._collect_done)

    def _show_notice(self, box, text):
        box.label.configure(text=text)
        box.pack(fill="x", pady=(12, 0))

    def _run_dart(self, app, put, out):
        from .dart import DartClient
        from .dart_collect import run
        from .dart_export import export
        key = app.keys.get("DART_API_KEY")
        if not key:
            return ["DART: 키가 없어 건너뜀"]
        api = app.http_factory("DART", float(app.settings.get("DART_CALLS_PER_SEC") or 5))
        base = app.con.execute("SELECT MAX(date) FROM universe_daily").fetchone()[0]
        targets = {r[0] for r in app.con.execute("SELECT code FROM universe_daily WHERE date=? AND in_universe=1", (base,))}
        st = run(app.con, DartClient(api, app.settings["DART_BASE_URL"], key), app.root, app.now,
                 int(app.settings.get("DART_DAILY_LIMIT") or 18000), targets, progress=lambda s: put(("log", s)))
        out["failures"] += [f"DART {e}" for e in st.errors]
        files = export(app.con, app.dirs["dart_export"], app.now())
        lines = [f"DART: 호출 {st.calls}회, 재무 받음 {st.fs_ok}, 실패 {st.fs_failed}, 남은 정기보고서 {st.remaining}건"]
        if st.stopped:
            lines.append("DART: " + st.stopped)
        lines += [f"DART 내보내기: {p.name}" for p in files]
        return lines

    def _collect_done(self, out):
        self._finish_progress()
        if out["stopped"]:
            self.c_status.configure(text=f"멈춤: {out['stopped']}", text_color=RED)
        else:
            n = len(out["failures"])
            self.c_status.configure(text="수집을 마쳤습니다." + (f" 건너뛴 항목 {n}건" if n else ""), text_color=NAVY)
        self._result_popup("수집 결과", out["lines"], out["root"], out["failures"], out["stopped"])

    def _result_popup(self, title, lines, folder: Path, failures=None, stopped=None):
        if isinstance(lines, str):
            lines = lines.splitlines()
        failures = failures or []
        w = ctk.CTkToplevel(self.root)
        w.title(title)
        w.configure(fg_color=WHITE)
        w.geometry("760x520")
        w.transient(self.root)
        w.after(50, w.grab_set)
        head = "멈췄습니다" if stopped else ("마쳤습니다" if not failures else f"마쳤습니다. 건너뛴 항목 {len(failures)}건")
        ctk.CTkLabel(w, text=f"{title}: {head}", font=F(18, "bold"), text_color=RED if stopped else NAVY,
                     anchor="w").pack(fill="x", padx=24, pady=(22, 10))
        box = ctk.CTkTextbox(w, fg_color=SURFACE, text_color=NAVY, font=F(12), corner_radius=8, wrap="word")
        box.pack(fill="both", expand=True, padx=24)
        box.insert("end", "\n".join(lines))
        if failures:
            box.insert("end", "\n\n건너뛴 항목\n" + "\n".join(failures[:100]))
        box.configure(state="disabled")
        bar = ctk.CTkFrame(w, fg_color=WHITE)
        bar.pack(fill="x", padx=24, pady=18)

        def open_and_close():
            L.open_folder(folder)
            w.destroy()

        ctk.CTkButton(bar, text="저장 폴더 열기", command=open_and_close, fg_color=COBALT, hover_color=COBALT_DARK,
                      font=F(13, "bold"), corner_radius=8, height=38, width=150).pack(side="right")
        self._secondary(bar, "닫기", w.destroy, width=90).pack(side="right", padx=10)

    # ---------------- [브리핑 데이터 가공] ----------------
    def _build_briefing(self):
        t = self.tab_brief
        self._title(t, "브리핑 데이터 가공",
                    "기준일을 고르면 그 날까지의 직전 250거래일 KRX·KIS 데이터로 브리핑용 xlsx 1개를 만듭니다. "
                    "API는 부르지 않고 저장된 데이터만 씁니다.")
        self._section(t, "기준일")
        row = ctk.CTkFrame(t, fg_color=WHITE)
        row.pack(fill="x")
        self.b_date = tk.StringVar(value=now_kst().strftime("%Y%m%d"))
        self._entry(row, self.b_date, width=140).pack(side="left")
        self._secondary(row, "마지막 수집일로", self._fill_last_date, width=130).pack(side="left", padx=10)
        self.b_json = tk.BooleanVar(value=False)
        self._check(t, "같은 내용을 JSON으로도 저장", self.b_json).pack(anchor="w", pady=(14, 0))
        self.btn_brief = self._primary(t, "브리핑 파일 만들기", self._on_briefing, width=200)
        self.btn_brief.pack(anchor="w", pady=(22, 0))
        self.b_notice = self._notice(t, "err")
        self.b_result = ctk.CTkLabel(t, text="", font=F(13), text_color=NAVY, anchor="w", justify="left", wraplength=720)
        self.b_result.pack(fill="x", pady=(18, 0))

    def _fill_last_date(self):
        def work(put):
            from .briefing import default_date
            app = self._start_app(put)
            try:
                return default_date(app.con)
            finally:
                app.close()

        def done(d):
            if d:
                self.b_date.set(d)
            else:
                self._show_notice(self.b_notice, "수집된 데이터가 아직 없습니다. [수집]에서 먼저 수집하세요.")
        self._run_worker(work, done)

    def _on_briefing(self):
        self.b_notice.pack_forget()
        day, with_json = self.b_date.get().strip(), self.b_json.get()
        if not re.fullmatch(r"\d{8}", day):
            self._show_notice(self.b_notice, "기준일은 20261119처럼 숫자 8자리로 입력하세요.")
            return
        self.b_result.configure(text="만드는 중")

        def work(put):
            from .briefing import BriefingError, build
            from .briefing_writer import record_unclassified, write
            app = self._start_app(put)
            try:
                now = app.now()
                try:
                    pkg = build(app.con, day, app.dirs["themes"], now, top_n=int(app.settings.get("UNCLASSIFIED_TOP_N") or 50))
                except BriefingError as exc:
                    return {"error": str(exc)}
                paths_ = write(pkg, app.dirs["briefing"], now, with_json=with_json)
                record_unclassified(app.con, pkg)
                return {"pkg": pkg, "paths": paths_, "folder": app.dirs["briefing"]}
            finally:
                app.close()

        self._run_worker(work, self._briefing_done)

    def _briefing_done(self, r):
        if r.get("error"):
            self.b_result.configure(text="")
            self._show_notice(self.b_notice, "브리핑 파일을 만들지 않았습니다. " + r["error"])
            return
        pkg = r["pkg"]
        lines = [f"기준일 {pkg.date}, 대상 종목 {len(pkg.stocks)}개, 테마 버전 {pkg.theme_version or '없음'}",
                 f"잠정값: 시세 {pkg.provisional['price']}종목, 수급 {pkg.provisional['investor']}종목"] + \
                [f"저장: {p}" for p in r["paths"]]
        self.b_result.configure(text="\n".join(lines))
        self._result_popup("브리핑 결과", lines, r["folder"])

    # ---------------- [테마 관리] ----------------
    def _themes_dir(self) -> Path:
        return paths.data_dir(load_settings(self.install)) / "themes"

    def _build_theme(self):
        t = self.tab_theme
        self._title(t, "테마 관리", "테마 마스터 파일을 불러오거나 저장합니다. 내용 편집은 엑셀 등에서 하세요.")
        self._section(t, "지금 쓰는 파일")
        self.t_info = ctk.CTkLabel(t, text="", font=F(13), text_color=NAVY, anchor="w", justify="left", wraplength=720)
        self.t_info.pack(fill="x")
        row = ctk.CTkFrame(t, fg_color=WHITE)
        row.pack(fill="x", pady=(22, 0))
        self._primary(row, "불러오기", self._theme_import, width=150).pack(side="left")
        self._secondary(row, "다른 이름으로 저장", self._theme_export, width=160).pack(side="left", padx=10)
        self._secondary(row, "폴더 열기", lambda: L.open_folder(self._themes_dir()), width=110).pack(side="left")
        self._section(t, "파일 형식")
        ctk.CTkLabel(t, text="theme,code,name,basis,added_date,note\n"
                             "HBM,000660,SK하이닉스,사업보고서 주요 제품,2026-11-20,\n\n"
                             "엑셀이 종목코드 앞의 0을 지워도 자동으로 채웁니다. 불러오면 이전 파일은 history 폴더에 보관합니다.",
                     font=F(12), text_color=GRAY, anchor="w", justify="left").pack(fill="x")

    def _refresh_theme_info(self):
        self.t_info.configure(text=L.theme_summary(self._themes_dir()))

    def _theme_import(self):
        src = filedialog.askopenfilename(title="테마 마스터 불러오기", filetypes=[("CSV", "*.csv"), ("모든 파일", "*.*")])
        if not src:
            return
        L.import_theme(Path(src), self._themes_dir(), now_kst())
        self._refresh_theme_info()
        messagebox.showinfo("불러오기", "불러왔습니다. 이전 파일은 themes/history에 보관했습니다.\n"
                                       "문제 있는 줄은 다음 브리핑의 6_데이터상태 시트에 나옵니다.")

    def _theme_export(self):
        dst = filedialog.asksaveasfilename(title="테마 마스터 저장", defaultextension=".csv",
                                           initialfile="theme_master.csv", filetypes=[("CSV", "*.csv")])
        if not dst:
            return
        try:
            L.export_theme(self._themes_dir(), Path(dst))
            messagebox.showinfo("저장", f"저장했습니다.\n{dst}")
        except FileNotFoundError as exc:
            messagebox.showerror("저장", str(exc))

    # ---------------- [설정] ----------------
    def _build_settings(self):
        t = self.tab_settings
        self._title(t, "설정")
        self._section(t, "API 키")
        ctk.CTkLabel(t, text="설치 폴더의 secret/keys.env에만 저장됩니다. 바꿀 키만 입력하고, 비워 두면 기존 값을 유지합니다.",
                     font=F(12), text_color=GRAY, anchor="w").pack(fill="x")
        grid = ctk.CTkFrame(t, fg_color=WHITE)
        grid.pack(fill="x", pady=(10, 0))
        self.k_vars, self.k_state = {}, {}
        for i, name in enumerate(KEY_NAMES):
            ctk.CTkLabel(grid, text=name, font=F(13), text_color=NAVY, width=150, anchor="w").grid(row=i, column=0, sticky="w", pady=4)
            v = tk.StringVar()
            self._entry(grid, v, width=360, show="•").grid(row=i, column=1, padx=10)
            s = ctk.CTkLabel(grid, text="", font=F(12), anchor="w")
            s.grid(row=i, column=2, sticky="w")
            self.k_vars[name], self.k_state[name] = v, s
        self._primary(t, "키 저장", self._save_keys, width=120).pack(anchor="w", pady=(12, 0))
        self._refresh_key_state()

        self._section(t, "글자 크기")
        labels = [f"{lbl} ({v}배)" for v, lbl in SCALES]
        cur = self.settings.get("GUI_FONT_SCALE") or "1.0"
        self.s_scale = tk.StringVar(value=next((l for l, (v, _) in zip(labels, SCALES) if v == cur), labels[0]))
        srow = ctk.CTkFrame(t, fg_color=WHITE)
        srow.pack(fill="x")
        for lbl in labels:
            ctk.CTkRadioButton(srow, text=lbl, value=lbl, variable=self.s_scale, command=self._on_scale, font=F(13),
                               text_color=NAVY, fg_color=COBALT, hover_color=COBALT_DARK, border_color=GRAY,
                               radiobutton_width=18, radiobutton_height=18).pack(side="left", padx=(0, 24))

        self._section(t, "기록과 폴더")
        row = ctk.CTkFrame(t, fg_color=WHITE)
        row.pack(fill="x")
        self._secondary(row, "작업 이력", self._show_history, width=120).pack(side="left")
        self._secondary(row, "데이터 폴더 열기",
                        lambda: L.open_folder(paths.data_dir(load_settings(self.install))), width=140).pack(side="left", padx=10)
        note = self._notice(t, "warn")
        note.label.configure(text="업데이트할 때 새 zip을 같은 위치에 덮어쓰세요. secret 폴더와 config/settings.env는 지우지 마세요.")
        note.pack(fill="x", pady=(24, 0))

    def _refresh_key_state(self):
        keys = load_keys(self.install)
        for name, lbl in self.k_state.items():
            v = keys.get(name)
            lbl.configure(text=f"저장됨 {mask(v)}" if v else "없음", text_color=GRAY if v else RED)

    def _save_keys(self):
        keys = load_keys(self.install)
        changed = []
        for name, var in self.k_vars.items():
            v = var.get().strip()
            if v:
                keys[name] = v
                register_sensitive(v)
                changed.append(name)
                var.set("")
        if not changed:
            messagebox.showinfo("키 저장", "바꿀 키를 입력한 뒤 눌러 주세요.")
            return
        save_keys(self.install, keys)
        self._refresh_key_state()
        self._refresh_nav_note()
        messagebox.showinfo("키 저장", "저장했습니다: " + ", ".join(changed))

    def _on_scale(self):
        label = self.s_scale.get()
        value = next(v for v, lbl in SCALES if label.startswith(lbl))
        set_setting(self.install, "GUI_FONT_SCALE", value)
        self.settings["GUI_FONT_SCALE"] = value
        self.scale = float(value)
        ctk.set_widget_scaling(self.scale)

    def _show_history(self):
        def work(put):
            app = self._start_app(put)
            try:
                return L.run_history(app.con)
            finally:
                app.close()

        def show(rows):
            w = ctk.CTkToplevel(self.root)
            w.title("작업 이력")
            w.configure(fg_color=WHITE)
            w.geometry("920x460")
            w.transient(self.root)
            ctk.CTkLabel(w, text="작업 이력", font=F(18, "bold"), text_color=NAVY, anchor="w").pack(fill="x", padx=24, pady=(20, 4))
            ctk.CTkLabel(w, text="줄을 두 번 누르면 그 실행의 요약을 봅니다.", font=F(12), text_color=GRAY, anchor="w").pack(fill="x", padx=24)
            st = ttk.Style(w)
            try:
                st.theme_use("clam")
            except tk.TclError:
                pass
            st.configure("H.Treeview", background=WHITE, fieldbackground=WHITE, foreground=NAVY,
                         rowheight=int(26 * self.scale), font=(FONT, int(10 * self.scale)), borderwidth=0)
            st.configure("H.Treeview.Heading", background=SURFACE, foreground=NAVY, font=(FONT, int(10 * self.scale), "bold"),
                         relief="flat")
            st.map("H.Treeview", background=[("selected", COBALT)], foreground=[("selected", WHITE)])
            cols = [("started", "수집 일시", 140), ("target", "대상 기간", 170), ("kinds", "데이터 종류", 110),
                    ("result", "결과", 90), ("off_hours", "시간대 밖", 80), ("calls", "호출 수", 80)]
            tv = ttk.Treeview(w, columns=[c for c, *_ in cols], show="headings", style="H.Treeview")
            for c, title, width in cols:
                tv.heading(c, text=title, anchor="w")
                tv.column(c, width=width, anchor="w")
            for i, r in enumerate(rows):
                tv.insert("", "end", iid=str(i), values=[r[c] for c, *_ in cols])
            tv.pack(fill="both", expand=True, padx=24, pady=16)
            tv.bind("<Double-1>", lambda e: tv.selection() and self._result_popup(
                "실행 요약", rows[int(tv.selection()[0])]["summary"] or ["(요약 없음)"],
                paths.data_dir(load_settings(self.install)) / "logs"))

        self._run_worker(work, show)


# ---------------- [백테스트] ----------------
# 수집·백필 잠금을 잡지 않고 백테스트 DB를 읽기 전용으로만 연다. API 키가 필요 없다.
def _bt_build(self):
    from .bt.cli import resolve_db
    t = self.tab_backtest
    self._title(t, "백테스트", "10년 과거 데이터로 추세추종 규칙을 계산만 합니다. 주문·매매는 하지 않습니다. "
                             "백필이 도는 중에도 실행할 수 있습니다.")
    row = ctk.CTkFrame(t, fg_color=WHITE)
    row.pack(fill="x", pady=(16, 0))
    self.bt_db = tk.StringVar(value=str(resolve_db(self.install, load_settings(self.install), None)))
    ctk.CTkLabel(row, text="DB", font=F(13, "bold"), text_color=NAVY, width=40, anchor="w").pack(side="left")
    ctk.CTkLabel(row, textvariable=self.bt_db, font=F(12), text_color=GRAY, anchor="w").pack(side="left", fill="x", expand=True)
    self._secondary(row, "DB 파일 선택", self._bt_pick_db, width=120).pack(side="right")
    self._section(t, "데이터 확보 현황")
    self.bt_cov = ctk.CTkLabel(t, text="", font=F(12), text_color=NAVY, anchor="w", justify="left", wraplength=760)
    self.bt_cov.pack(fill="x")
    vrow = ctk.CTkFrame(t, fg_color=WHITE)
    vrow.pack(fill="x", pady=(14, 0))
    ctk.CTkLabel(vrow, text="변형", font=F(13, "bold"), text_color=NAVY, width=40, anchor="w").pack(side="left")
    self.bt_var = tk.StringVar(value="all")
    for val, lbl in (("A", "A"), ("A4", "A4 개선"), ("B", "B +수급"), ("C", "C +재무"), ("D", "D +지수"), ("all", "전체")):
        ctk.CTkRadioButton(vrow, text=lbl, value=val, variable=self.bt_var, font=F(13), text_color=NAVY, fg_color=COBALT,
                           hover_color=COBALT_DARK, border_color=GRAY, radiobutton_width=18,
                           radiobutton_height=18).pack(side="left", padx=(0, 18))
    brow = ctk.CTkFrame(t, fg_color=WHITE)
    brow.pack(fill="x", pady=(16, 0))
    self.btn_bt = self._primary(brow, "실행", self._bt_run, width=140)
    self.btn_bt.pack(side="left")
    self._secondary(brow, "결과 폴더 열기", lambda: L.open_folder(Path(self.bt_db.get()).parent / "results"), width=130).pack(side="left", padx=(10, 0))
    b1 = self._secondary(brow, "최근 데이터 합치기", lambda: self._bt_sync(False), width=150)
    b1.pack(side="left", padx=(10, 0))
    b2 = self._secondary(brow, "업로드용 내보내기", lambda: self._bt_sync(True), width=150)
    b2.pack(side="left", padx=(10, 0))
    b3 = self._secondary(brow, "신호 분석", self._bt_signals, width=110)
    b3.pack(side="left", padx=(10, 0))
    self._action_buttons += [b1, b2, b3]
    self.bt_progress = ctk.CTkProgressBar(t, height=8, corner_radius=4, fg_color=SURFACE, progress_color=COBALT)
    self.bt_progress.set(0)
    self.bt_progress.pack(fill="x", pady=(14, 0))
    self.bt_status = ctk.CTkLabel(t, text="", font=F(12), text_color=GRAY, anchor="w")
    self.bt_status.pack(fill="x", pady=(4, 0))
    self.bt_result = ctk.CTkTextbox(t, fg_color=SURFACE, text_color=NAVY, corner_radius=8, font=F(12), wrap="none")
    self.bt_result.pack(fill="both", expand=True, pady=(8, 0))
    self.bt_result.configure(state="disabled")
    self._bt_refresh_cov()


def _bt_refresh_cov(self):
    from .bt.data import coverage, coverage_lines, open_ro
    p = Path(self.bt_db.get())
    try:
        con = open_ro(p)
        try:
            self.bt_cov.configure(text="\n".join(coverage_lines(coverage(con))))
        finally:
            con.close()
    except Exception as exc:
        self.bt_cov.configure(text=f"DB를 읽지 못했습니다: {exc}\n[DB 파일 선택]으로 backtest.sqlite를 고르세요.")


def _bt_pick_db(self):
    f = filedialog.askopenfilename(title="백테스트 DB 선택", filetypes=[("SQLite", "*.sqlite"), ("모든 파일", "*.*")])
    if f:
        set_setting(self.install, "BACKTEST_DB_PATH", f)
        self.bt_db.set(f)
        self._bt_refresh_cov()


def _bt_set_result(self, text):
    self.bt_result.configure(state="normal")
    self.bt_result.delete("1.0", "end")
    self.bt_result.insert("end", text)
    self.bt_result.configure(state="disabled")


def _bt_run(self):
    from .bt import config as C
    from .bt.cli import themes_csv_for
    from .bt.run import run_backtest
    v = self.bt_var.get()
    from .bt.engine import ALL_VARIANTS
    variants = list(ALL_VARIANTS) if v == "all" else [v]
    db_path = Path(self.bt_db.get())
    self.bt_progress.set(0)

    def work(put):
        cfg = C.load(self.install)
        return run_backtest(db_path, cfg, variants, now_kst(), progress=lambda s, x: put(("bt", s, x)),
                            themes_csv=themes_csv_for(db_path, load_settings(self.install)))

    def done(out):
        self.bt_progress.set(1)
        self.bt_status.configure(text=f"완료: {out}", text_color=NAVY)
        self._bt_set_result((out / "summary.md").read_text(encoding="utf-8"))
        self._bt_refresh_cov()
    self._run_worker(work, done)


def _bt_signals(self):
    from .bt import config as C
    from .bt.cli import themes_csv_for
    from .bt.signals import run_signals
    db_path = Path(self.bt_db.get())
    self.bt_progress.set(0)

    def work(put):
        return run_signals(db_path, C.load(self.install), now_kst(), progress=lambda s, x: put(("bt", s, x)),
                           themes_csv=themes_csv_for(db_path, load_settings(self.install)))

    def done(out):
        self.bt_progress.set(1)
        self.bt_status.configure(text=f"신호 분석 완료: {out}", text_color=NAVY)
        self._bt_set_result((out / "signals.md").read_text(encoding="utf-8"))
    self._run_worker(work, done)


def _bt_sync(self, export: bool):
    from . import paths as P
    from .bt.sync import export as do_export, sync
    db_path = Path(self.bt_db.get())
    settings = load_settings(self.install)

    def work(put):
        root = P.data_dir(settings)
        lines, rep = sync(db_path, root / "db" / "market.sqlite",
                          (root if root.exists() else db_path.parent) / "system" / "verify", now_kst())
        if export:
            lines.append(f"내보내기: {do_export(db_path, P.desktop_dir(), now_kst())}")
        return lines

    def done(lines):
        self._bt_set_result("\n".join(lines))
        self.bt_status.configure(text="내보내기를 마쳤습니다." if export else "합치기를 마쳤습니다.", text_color=NAVY)
        self._bt_refresh_cov()
    self.bt_status.configure(text="합치는 중" + (" (끝나면 내보내기)" if export else ""), text_color=NAVY)
    self._run_worker(work, done)


for _n in ("_bt_build", "_bt_refresh_cov", "_bt_pick_db", "_bt_set_result", "_bt_run", "_bt_sync", "_bt_signals"):
    setattr(MainWindow, "_build_backtest" if _n == "_bt_build" else _n, globals()[_n])


def run_gui() -> int:
    if sys.stdout is None:  # 창 전용 exe에는 콘솔이 없다
        import os
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
    load_fonts()
    root = ctk.CTk()
    MainWindow(root)
    root.mainloop()
    return 0

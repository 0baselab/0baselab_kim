"""GUI (UI/UX 결정사항 2026-11-19): 탭 4개 [수집] [브리핑 데이터 가공] [테마 관리] [설정].

작업은 별도 스레드에서 돌리고, 화면 갱신은 큐를 통해 메인 스레드에서만 한다.
DB 연결은 작업 스레드 안에서 만들고 닫는다(실행 중 잠금 파일도 그때만 잡는다).
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter.font as tkfont

from . import gui_logic as L
from . import paths
from .secrets import KEY_NAMES, load_keys, mask, register_sensitive, save_keys
from .settings import load_settings, set_setting
from .timeutil import now_kst
from .version import __version__

NAVY, COBALT, GRAY, WHITE = "#0B1F3A", "#0047FF", "#8A94A6", "#FFFFFF"
RED, YELLOW, LIGHT = "#D92D20", "#F5B800", "#F3F5F9"
SCALES = [("1.0", "보통"), ("1.2", "크게"), ("1.4", "아주 크게")]
PROGRESS_RE = re.compile(r"(\d+)/(\d+)")


class QueueLogHandler(logging.Handler):
    def __init__(self, q):
        super().__init__(logging.INFO)
        self.q = q

    def emit(self, record):
        try:
            self.q.put(("log", self.format(record)))
        except Exception:
            pass


class MainWindow:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.install = paths.install_dir()
        self.settings = load_settings(self.install)
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.last_log_path: Path | None = None
        root.title(f"주식데이터수집기 {__version__}")
        root.geometry("980x700")
        root.minsize(820, 560)
        root.configure(bg=WHITE)
        self._fonts()
        self._style()
        self._header()
        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        self.tab_collect = self._tab("수집")
        self.tab_brief = self._tab("브리핑 데이터 가공")
        self.tab_theme = self._tab("테마 관리")
        self.tab_settings = self._tab("설정")
        self._build_collect()
        self._build_briefing()
        self._build_theme()
        self._build_settings()
        root.protocol("WM_DELETE_WINDOW", self._on_close)
        root.after(100, self._poll)
        root.after(300, self._startup_checks)

    # ---------------- 모양 ----------------
    def _fonts(self):
        scale = float(self.settings.get("GUI_FONT_SCALE") or 1.0)
        self.scale = scale
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkFixedFont"):
            f = tkfont.nametofont(name)
            f.configure(size=int(round(10 * scale)))
        self.f_title = tkfont.Font(size=int(round(14 * scale)), weight="bold")
        self.f_bold = tkfont.Font(size=int(round(10 * scale)), weight="bold")

    def _style(self):
        st = ttk.Style(self.root)
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=WHITE, foreground=NAVY)
        st.configure("TNotebook", background=WHITE, borderwidth=0)
        st.configure("TNotebook.Tab", padding=(16, 8), background=LIGHT, foreground=GRAY)
        st.map("TNotebook.Tab", background=[("selected", WHITE)], foreground=[("selected", NAVY)])
        st.configure("TFrame", background=WHITE)
        st.configure("TLabel", background=WHITE, foreground=NAVY)
        st.configure("Muted.TLabel", foreground=GRAY)
        st.configure("Warn.TLabel", foreground="#8A6100")
        st.configure("Err.TLabel", foreground=RED)
        st.configure("TCheckbutton", background=WHITE, foreground=NAVY)
        st.configure("Primary.TButton", background=COBALT, foreground=WHITE, padding=(18, 8), borderwidth=0)
        st.map("Primary.TButton", background=[("disabled", GRAY), ("active", "#0036C4")])
        st.configure("TButton", padding=(12, 6))
        st.configure("Horizontal.TProgressbar", background=COBALT, troughcolor=LIGHT)
        st.configure("Treeview", rowheight=int(22 * self.scale))

    def _header(self):
        bar = tk.Frame(self.root, bg=NAVY, height=48)
        bar.pack(fill="x")
        tk.Label(bar, text="주식데이터수집기", bg=NAVY, fg=WHITE, font=self.f_title).pack(side="left", padx=16, pady=10)
        tk.Label(bar, text=f"v{__version__}", bg=NAVY, fg=GRAY).pack(side="left")
        tk.Frame(self.root, bg=WHITE, height=8).pack(fill="x")

    def _tab(self, title):
        f = ttk.Frame(self.nb, padding=16)
        self.nb.add(f, text=title)
        return f

    # ---------------- 작업 실행 공통 ----------------
    def _run_worker(self, fn, done):
        """fn(app_factory, put) 을 작업 스레드에서 실행하고, 결과를 done(result)로 메인 스레드에 돌려준다."""
        if self.busy:
            messagebox.showinfo("실행 중", "다른 작업이 진행 중입니다. 끝난 뒤 다시 눌러 주세요.")
            return
        self.busy = True
        self._set_buttons("disabled")

        def target():
            try:
                result = fn(self.q.put)
                self.q.put(("done", done, result, None))
            except Exception as exc:  # 화면이 멈추지 않게 오류도 돌려준다
                logging.getLogger("collector").exception("작업 오류")
                self.q.put(("done", done, None, exc))

        threading.Thread(target=target, daemon=True).start()

    def _start_app(self, put):
        from .app import start
        app = start(prompt_keys=False, out=lambda s: put(("log", s)))
        h = QueueLogHandler(self.q)
        h.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
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
                kind = msg[0]
                if kind == "log":
                    self._log(msg[1])
                elif kind == "ask":
                    _, question, ev, holder = msg
                    holder["answer"] = messagebox.askyesno("확인", question)
                    ev.set()
                elif kind == "done":
                    _, done, result, exc = msg
                    self.busy = False
                    self._set_buttons("normal")
                    self.progress.stop()
                    if exc is not None:
                        messagebox.showerror("오류", f"{type(exc).__name__}: {exc}")
                    else:
                        done(result)
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def _set_buttons(self, state):
        for b in getattr(self, "_action_buttons", []):
            b.configure(state=state)

    def _on_close(self):
        if self.busy and not messagebox.askyesno(
                "종료", "작업이 진행 중입니다. 지금 끄면 진행 중인 단위만 버리고, 다음 실행 때 이어서 받습니다. 종료할까요?"):
            return
        self.root.destroy()

    def _startup_checks(self):
        changed = L.version_changed(self.install)
        miss = L.missing_keys(self.install)
        if miss:
            head = "업데이트 후 첫 실행입니다. " if changed else ""
            messagebox.showwarning("API 키 확인", head + "다음 키가 없습니다: " + ", ".join(miss) +
                                   "\n[설정] 탭에서 입력해 주세요.")
            self.nb.select(self.tab_settings)
        elif changed:
            self._log(f"업데이트 확인: v{__version__}, API 키 4개 모두 있음")

    # ---------------- [수집] ----------------
    def _build_collect(self):
        t = self.tab_collect
        today = now_kst().strftime("%Y%m%d")
        row = ttk.Frame(t)
        row.pack(fill="x")
        ttk.Label(row, text="기간", font=self.f_bold).pack(side="left")
        self.c_start = tk.StringVar(value=today)
        self.c_end = tk.StringVar(value=today)
        ttk.Entry(row, textvariable=self.c_start, width=10).pack(side="left", padx=(8, 4))
        ttk.Label(row, text="~").pack(side="left")
        ttk.Entry(row, textvariable=self.c_end, width=10).pack(side="left", padx=4)
        ttk.Label(row, text="YYYYMMDD. 오늘 하루면 빠진 날도 자동으로 채웁니다.", style="Muted.TLabel").pack(side="left", padx=8)
        row2 = ttk.Frame(t)
        row2.pack(fill="x", pady=(10, 0))
        ttk.Label(row2, text="데이터", font=self.f_bold).pack(side="left")
        self.c_kis, self.c_krx, self.c_dart = tk.BooleanVar(value=True), tk.BooleanVar(value=True), tk.BooleanVar(value=False)
        for text, var in (("KIS", self.c_kis), ("KRX", self.c_krx), ("DART", self.c_dart)):
            ttk.Checkbutton(row2, text=text, variable=var).pack(side="left", padx=(8, 4))
        self.c_refetch = tk.BooleanVar(value=False)
        ttk.Checkbutton(row2, text="'시간대 밖' 표시된 데이터도 다시 받기", variable=self.c_refetch).pack(side="left", padx=16)
        row3 = ttk.Frame(t)
        row3.pack(fill="x", pady=12)
        self.btn_collect = ttk.Button(row3, text="수집 실행", style="Primary.TButton", command=self._on_collect)
        self.btn_collect.pack(side="left")
        self.c_status = ttk.Label(row3, text="권장 수집 시간: 장 열린 날 20:30~22:00", style="Muted.TLabel")
        self.c_status.pack(side="left", padx=12)
        self.progress = ttk.Progressbar(t, mode="indeterminate")
        self.progress.pack(fill="x")
        logf = ttk.Frame(t)
        logf.pack(fill="both", expand=True, pady=(10, 0))
        self.logbox = tk.Text(logf, height=16, wrap="word", bg=LIGHT, fg=NAVY, relief="flat", state="disabled")
        sb = ttk.Scrollbar(logf, command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=sb.set)
        self.logbox.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        ttk.Button(t, text="상세 로그 복사", command=self._copy_log).pack(anchor="e", pady=(8, 0))
        self._action_buttons = [self.btn_collect]

    def _log(self, text):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", text.rstrip() + "\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")
        m = PROGRESS_RE.search(text)
        if m and ("KRX" in text or "KIS" in text or "DART" in text):
            a, b = int(m.group(1)), int(m.group(2))
            self.progress.stop()
            self.progress.configure(mode="determinate", maximum=max(b, 1), value=a)
            self.c_status.configure(text=text.strip(), style="TLabel")

    def _copy_log(self):
        text = ""
        if self.last_log_path and self.last_log_path.exists():
            text = self.last_log_path.read_text(encoding="utf-8", errors="replace")
        if not text:
            text = self.logbox.get("1.0", "end")
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        messagebox.showinfo("복사", "상세 로그를 클립보드에 복사했습니다.")

    def _on_collect(self):
        from .collect import in_recommended_window
        start, end = self.c_start.get().strip(), self.c_end.get().strip()
        if not (re.fullmatch(r"\d{8}", start) and re.fullmatch(r"\d{8}", end)) or start > end:
            messagebox.showerror("기간 오류", "기간은 YYYYMMDD 형식이고, 시작일이 종료일보다 늦으면 안 됩니다.")
            return
        if not (self.c_kis.get() or self.c_krx.get() or self.c_dart.get()):
            messagebox.showerror("데이터 선택", "데이터 종류를 하나 이상 고르세요.")
            return
        if not in_recommended_window(now_kst()) and (self.c_kis.get() or self.c_krx.get()):
            if not messagebox.askyesno(
                    "권장 시간대 밖", "지금은 권장 수집 시간(장 열린 날 20:30~22:00)이 아닙니다.\n"
                    "이 시간에 받은 데이터는 권장 시간대에 받는 데이터와 다를 수 있습니다.\n"
                    "이번 수집 기록에는 '시간대 밖' 표시가 남고, 나중에 다시 받을 수 있습니다.\n\n계속할까요?",
                    icon="warning"):
                return
        mode, s, e = L.collect_mode(start, end, now_kst().strftime("%Y%m%d"))
        opts = dict(kis=self.c_kis.get(), krx=self.c_krx.get(), dart=self.c_dart.get(), refetch=self.c_refetch.get())
        self.logbox.configure(state="normal")
        self.logbox.delete("1.0", "end")
        self.logbox.configure(state="disabled")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        self.c_status.configure(text="수집 중...", style="TLabel")

        def work(put):
            from .collect import run_collect
            app = self._start_app(put)
            out = {"lines": [], "failures": [], "root": app.root, "stopped": None}
            try:
                if opts["kis"] or opts["krx"]:
                    # 시간대 경고는 시작 전에 이미 했으므로 애프터마켓 질문은 다시 묻지 않는다
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
        self.progress.configure(mode="determinate", value=self.progress["maximum"])
        self.c_status.configure(text="완료" if not out["stopped"] else f"멈춤: {out['stopped']}",
                                style="TLabel" if not out["stopped"] else "Warn.TLabel")
        body = "\n".join(out["lines"])
        if out["failures"]:
            body += "\n\n건너뛴 항목 (" + str(len(out["failures"])) + "건)\n" + "\n".join(out["failures"][:50])
        self._result_popup("수집 결과", body, out["root"])

    def _result_popup(self, title, body, folder: Path):
        w = tk.Toplevel(self.root)
        w.title(title)
        w.configure(bg=WHITE)
        w.transient(self.root)
        txt = tk.Text(w, width=90, height=20, wrap="word", bg=LIGHT, fg=NAVY, relief="flat")
        txt.insert("1.0", body)
        txt.configure(state="disabled")
        txt.pack(fill="both", expand=True, padx=12, pady=12)
        bar = ttk.Frame(w)
        bar.pack(fill="x", padx=12, pady=(0, 12))

        def open_and_close():
            L.open_folder(folder)
            w.destroy()

        ttk.Button(bar, text="닫기", command=w.destroy).pack(side="right")
        ttk.Button(bar, text="저장 폴더 열기", style="Primary.TButton", command=open_and_close).pack(side="right", padx=8)

    # ---------------- [브리핑 데이터 가공] ----------------
    def _build_briefing(self):
        t = self.tab_brief
        ttk.Label(t, text="기준일을 고르면, 그 날까지의 수집 데이터(직전 250거래일 KRX·KIS)로 브리핑용 xlsx 1개를 만듭니다.",
                  style="Muted.TLabel").pack(anchor="w")
        row = ttk.Frame(t)
        row.pack(fill="x", pady=12)
        ttk.Label(row, text="기준일", font=self.f_bold).pack(side="left")
        self.b_date = tk.StringVar(value=now_kst().strftime("%Y%m%d"))
        ttk.Entry(row, textvariable=self.b_date, width=10).pack(side="left", padx=8)
        ttk.Button(row, text="마지막 수집일", command=self._fill_last_date).pack(side="left")
        self.b_json = tk.BooleanVar(value=False)
        ttk.Checkbutton(row, text="JSON도 저장", variable=self.b_json).pack(side="left", padx=16)
        self.btn_brief = ttk.Button(t, text="브리핑 파일 만들기", style="Primary.TButton", command=self._on_briefing)
        self.btn_brief.pack(anchor="w")
        self.b_status = ttk.Label(t, text="", style="Muted.TLabel", wraplength=800, justify="left")
        self.b_status.pack(anchor="w", pady=12)
        self._action_buttons.append(self.btn_brief)

    def _fill_last_date(self):
        def work(put):
            from .briefing import default_date
            app = self._start_app(put)
            try:
                return default_date(app.con)
            finally:
                app.close()
        self._run_worker(work, lambda d: self.b_date.set(d) if d else messagebox.showinfo("기준일", "수집된 데이터가 없습니다."))

    def _on_briefing(self):
        day, with_json = self.b_date.get().strip(), self.b_json.get()
        if not re.fullmatch(r"\d{8}", day):
            messagebox.showerror("기준일 오류", "기준일은 YYYYMMDD 형식으로 입력하세요.")
            return
        self.b_status.configure(text="만드는 중...")

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
            self.b_status.configure(text="만들지 않았습니다: " + r["error"], style="Err.TLabel")
            return
        pkg = r["pkg"]
        body = (f"기준일 {pkg.date}, 대상 종목 {len(pkg.stocks)}개, 테마 버전 {pkg.theme_version or '없음'}\n"
                f"잠정값: 시세 {pkg.provisional['price']}종목, 수급 {pkg.provisional['investor']}종목\n\n" +
                "\n".join(f"저장: {p}" for p in r["paths"]))
        self.b_status.configure(text=body, style="TLabel")
        self._result_popup("브리핑 결과", body, r["folder"])

    # ---------------- [테마 관리] ----------------
    def _themes_dir(self) -> Path:
        return paths.data_dir(load_settings(self.install)) / "themes"

    def _build_theme(self):
        t = self.tab_theme
        ttk.Label(t, text="테마 마스터 파일(theme_master.csv)을 불러오거나 저장합니다. 편집은 엑셀 등에서 하세요.",
                  style="Muted.TLabel").pack(anchor="w")
        self.t_info = ttk.Label(t, text="", wraplength=800, justify="left")
        self.t_info.pack(anchor="w", pady=12)
        row = ttk.Frame(t)
        row.pack(anchor="w")
        ttk.Button(row, text="불러오기...", style="Primary.TButton", command=self._theme_import).pack(side="left")
        ttk.Button(row, text="다른 이름으로 저장...", command=self._theme_export).pack(side="left", padx=8)
        ttk.Button(row, text="폴더 열기", command=lambda: L.open_folder(self._themes_dir())).pack(side="left")
        ttk.Label(t, text="형식: theme,code,name,basis,added_date,note  (종목코드 앞의 0이 지워져도 자동 보정)",
                  style="Muted.TLabel").pack(anchor="w", pady=12)
        self._refresh_theme_info()

    def _refresh_theme_info(self):
        self.t_info.configure(text=L.theme_summary(self._themes_dir()))

    def _theme_import(self):
        src = filedialog.askopenfilename(title="테마 마스터 불러오기", filetypes=[("CSV", "*.csv"), ("모든 파일", "*.*")])
        if not src:
            return
        L.import_theme(Path(src), self._themes_dir(), now_kst())
        self._refresh_theme_info()
        messagebox.showinfo("불러오기", "불러왔습니다. 이전 파일은 themes/history에 보관했습니다.\n"
                                       "검사 결과(문제 줄)는 다음 브리핑의 6_데이터상태 시트에 나옵니다.")

    def _theme_export(self):
        dst = filedialog.asksaveasfilename(title="테마 마스터 저장", defaultextension=".csv",
                                           initialfile="theme_master.csv", filetypes=[("CSV", "*.csv")])
        if not dst:
            return
        try:
            L.export_theme(self._themes_dir(), Path(dst))
            messagebox.showinfo("저장", f"저장했습니다: {dst}")
        except FileNotFoundError as exc:
            messagebox.showerror("저장", str(exc))

    # ---------------- [설정] ----------------
    def _build_settings(self):
        t = self.tab_settings
        ttk.Label(t, text="API 키", font=self.f_bold).pack(anchor="w")
        ttk.Label(t, text=f"설치 폴더의 secret/keys.env에만 저장됩니다. 비워 두면 기존 값을 유지합니다.",
                  style="Muted.TLabel").pack(anchor="w")
        grid = ttk.Frame(t)
        grid.pack(anchor="w", pady=8)
        self.k_vars, self.k_state = {}, {}
        for i, name in enumerate(KEY_NAMES):
            ttk.Label(grid, text=name).grid(row=i, column=0, sticky="w", pady=3)
            v = tk.StringVar()
            ttk.Entry(grid, textvariable=v, show="•", width=44).grid(row=i, column=1, padx=8)
            s = ttk.Label(grid, text="", style="Muted.TLabel")
            s.grid(row=i, column=2, sticky="w")
            self.k_vars[name], self.k_state[name] = v, s
        ttk.Button(t, text="키 저장", style="Primary.TButton", command=self._save_keys).pack(anchor="w")
        self._refresh_key_state()
        ttk.Separator(t).pack(fill="x", pady=16)
        row = ttk.Frame(t)
        row.pack(anchor="w")
        ttk.Label(row, text="글자 크기", font=self.f_bold).pack(side="left")
        cur = self.settings.get("GUI_FONT_SCALE") or "1.0"
        labels = [f"{lbl} ({v}배)" for v, lbl in SCALES]
        self.s_scale = tk.StringVar(value=next((l for l, (v, _) in zip(labels, SCALES) if v == cur), labels[0]))
        cb = ttk.Combobox(row, textvariable=self.s_scale, values=labels, state="readonly", width=16)
        cb.pack(side="left", padx=8)
        cb.bind("<<ComboboxSelected>>", self._on_scale)
        ttk.Separator(t).pack(fill="x", pady=16)
        row2 = ttk.Frame(t)
        row2.pack(anchor="w")
        ttk.Button(row2, text="작업 이력", command=self._show_history).pack(side="left")
        ttk.Button(row2, text="데이터 폴더 열기",
                   command=lambda: L.open_folder(paths.data_dir(load_settings(self.install)))).pack(side="left", padx=8)
        ttk.Label(t, text="업데이트할 때 새 zip을 같은 위치에 덮어쓰세요. secret 폴더와 config/settings.env는 지우지 마세요.",
                  style="Warn.TLabel").pack(anchor="w", pady=16)

    def _refresh_key_state(self):
        keys = load_keys(self.install)
        for name, lbl in self.k_state.items():
            v = keys.get(name)
            lbl.configure(text=f"저장됨 ({mask(v)})" if v else "없음", style="Muted.TLabel" if v else "Err.TLabel")

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
            messagebox.showinfo("키 저장", "입력된 키가 없습니다.")
            return
        save_keys(self.install, keys)
        self._refresh_key_state()
        messagebox.showinfo("키 저장", "저장했습니다: " + ", ".join(changed))

    def _on_scale(self, _evt=None):
        label = self.s_scale.get()
        value = next(v for v, lbl in SCALES if label.startswith(lbl))
        set_setting(self.install, "GUI_FONT_SCALE", value)
        self.settings["GUI_FONT_SCALE"] = value
        self._fonts()
        self._style()

    def _show_history(self):
        def work(put):
            app = self._start_app(put)
            try:
                return L.run_history(app.con)
            finally:
                app.close()

        def show(rows):
            w = tk.Toplevel(self.root)
            w.title("작업 이력")
            w.geometry("900x420")
            cols = [("started", "수집 일시", 140), ("target", "대상 기간", 150), ("kinds", "데이터 종류", 100),
                    ("result", "결과", 80), ("off_hours", "시간대 밖", 70), ("calls", "호출 수", 70)]
            tv = ttk.Treeview(w, columns=[c for c, *_ in cols], show="headings")
            for c, title, width in cols:
                tv.heading(c, text=title)
                tv.column(c, width=width, anchor="w")
            for i, r in enumerate(rows):
                tv.insert("", "end", iid=str(i), values=[r[c] for c, *_ in cols])
            tv.pack(fill="both", expand=True, padx=12, pady=12)
            ttk.Label(w, text="줄을 두 번 누르면 실행 요약을 봅니다.", style="Muted.TLabel").pack(anchor="w", padx=12, pady=(0, 8))
            tv.bind("<Double-1>", lambda e: tv.selection() and self._result_popup(
                "실행 요약", "\n".join(rows[int(tv.selection()[0])]["summary"]) or "(요약 없음)",
                paths.data_dir(load_settings(self.install)) / "logs"))

        self._run_worker(work, show)


def run_gui() -> int:
    L.hide_console_on_windows()
    root = tk.Tk()
    MainWindow(root)
    root.mainloop()
    return 0

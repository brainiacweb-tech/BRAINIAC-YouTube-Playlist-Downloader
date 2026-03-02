import os
import threading
import winreg
import tkinter as tk
from tkinter import ttk, font as tkfont, filedialog, messagebox

import yt_dlp

# Paths
DOWNLOADS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# ── Cloud folder detection ──────────────────────────────────────────────────
def _find_onedrive():
    """Return the user's local OneDrive sync root, or None."""
    # Env var set by OneDrive client
    p = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
    if p and os.path.isdir(p):
        return p
    # Registry fallback
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\OneDrive")
        p, _ = winreg.QueryValueEx(key, "UserFolder")
        winreg.CloseKey(key)
        if p and os.path.isdir(p):
            return p
    except Exception:
        pass
    # Common default path
    p = os.path.join(os.path.expanduser("~"), "OneDrive")
    return p if os.path.isdir(p) else None


def _find_googledrive():
    """Return the user's local Google Drive 'My Drive' folder, or None.
    Strategy:
    1. Parse the DriveFS registry JSON to get all configured mount-point letters.
    2. Scan every drive letter A-Z for a 'My Drive' sub-folder (handles any letter).
    3. Fall back to common hard-coded paths.
    """
    import json, string
    candidates = []

    # Read mount-point drive letter(s) from DriveFS registry JSON
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Google\DriveFS")
        raw, _ = winreg.QueryValueEx(key, "PerAccountPreferences")
        winreg.CloseKey(key)
        data = json.loads(raw)
        for entry in data.get("per_account_preferences", []):
            mp = entry.get("value", {}).get("mount_point_path", "")
            if mp:
                # mp is a single drive letter, e.g. "G" or "H"
                candidates.append(f"{mp.rstrip(chr(92)).rstrip(':')}:\\My Drive")
    except Exception:
        pass

    # Scan every drive letter for a My Drive subfolder
    for letter in string.ascii_uppercase:
        p = f"{letter}:\\My Drive"
        if p not in candidates:
            candidates.append(p)

    # Legacy / fallback paths
    for extra in [
        os.path.join(os.path.expanduser("~"), "Google Drive", "My Drive"),
        os.path.join(os.path.expanduser("~"), "Google Drive"),
        os.path.join(os.path.expanduser("~"), "My Drive"),
    ]:
        candidates.append(extra)

    for p in candidates:
        if os.path.isdir(p):
            return p
    return None



BG     = "#0a0e14"
PANEL  = "#111620"
CARD   = "#161d2a"
CARD2  = "#1c2333"
BORDER = "#252f42"
RED    = "#e63946"
RED2   = "#c1121f"
GREEN  = "#2ec27e"
BLUE   = "#4dabf7"
YELLOW = "#ffd43b"
PURPLE = "#9775fa"
FG     = "#e8edf5"
MUTED  = "#a8b5cc"
WHITE  = "#ffffff"

def _f(size, weight="normal"):
    for fam in ("Montserrat", "Montserrat Medium", "Segoe UI"):
        try:
            t = tkfont.Font(family=fam, size=size, weight=weight)
            if t.actual("family").lower().startswith("mont") or fam == "Segoe UI":
                return (fam, size, weight)
        except Exception:
            pass
    return ("Segoe UI", size, weight)

F_TITLE = _f(14,"bold"); F_HEAD = _f(11,"bold"); F_BODY = _f(10,"bold")
F_SMALL = _f(9,"bold");   F_TINY = _f(8,"bold");  F_LABEL = _f(9,"bold")
F_LOG   = ("Consolas", 9, "bold")

FFMPEG_LOC = None
for _p in [os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Links"),
           r"C:\ffmpeg\bin", r"C:\Program Files\ffmpeg\bin"]:
    if os.path.isdir(_p):
        FFMPEG_LOC = _p; break


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("BRAINIAC MUSIC DOWNLOADER")
        self.geometry("900x700")
        self.minsize(740, 560)
        self.configure(bg=BG)
        self.resizable(True, True)
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        self._is_downloading = False
        self._completed = 0
        self._errors = 0
        self._save_dir = DOWNLOADS_DIR
        self.quality_var = tk.StringVar(value="Best Quality")
        self._pause_evt = threading.Event()
        self._pause_evt.set()
        self._paused = False
        self._cloud_mirror = None   # if set, finished files are also copied here
        self._cloud_label  = ""     # friendly name for the mirror (OneDrive / Google Drive)
        self._mode = "music"         # "playlist" or "music"
        self._build_ui()
        self.after(50, lambda: self._switch_mode("music"))

    def _build_ui(self):
        self._build_sidebar()
        self._build_main()

    # ── SIDEBAR ─────────────────────────────────────────────────────────────
    def _build_sidebar(self):
        sb = tk.Frame(self, bg=PANEL, width=240)
        sb.pack(side="left", fill="y")
        sb.pack_propagate(False)

        # Logo
        logo = tk.Frame(sb, bg=RED, height=70)
        logo.pack(fill="x")
        logo.pack_propagate(False)
        tk.Label(logo, text="▶  BRAINIAC", bg=RED, fg=WHITE,
                 font=_f(13,"bold")).place(relx=.5, rely=.5, anchor="center")

        tk.Frame(sb, bg=BORDER, height=1).pack(fill="x")

        # Footer (pinned at bottom BEFORE the scrollable area so it always shows)
        tk.Label(sb, text="Developed by Francis Kusi", bg=PANEL, fg=WHITE,
                 font=_f(11,"bold"), justify="center").pack(side="bottom", pady=10)
        tk.Frame(sb, bg=BORDER, height=1).pack(side="bottom", fill="x")

        # Scrollable inner area
        sb_cvs = tk.Canvas(sb, bg=PANEL, highlightthickness=0, bd=0)
        sb_vsb = ttk.Scrollbar(sb, orient="vertical", command=sb_cvs.yview)
        sb_cvs.configure(yscrollcommand=sb_vsb.set)
        sb_vsb.pack(side="right", fill="y")
        sb_cvs.pack(side="left", fill="both", expand=True)

        inner = tk.Frame(sb_cvs, bg=PANEL)
        iwin  = sb_cvs.create_window((0, 0), window=inner, anchor="nw")
        sb_cvs.bind("<Configure>", lambda e: sb_cvs.itemconfig(iwin, width=e.width))
        inner.bind("<Configure>",  lambda e: sb_cvs.configure(scrollregion=sb_cvs.bbox("all")))
        sb_cvs.bind("<MouseWheel>", lambda e: sb_cvs.yview_scroll(int(-1*(e.delta/120)), "units"))

        # ── Stats
        pi = dict(padx=14, pady=(0,6))
        tk.Label(inner, text="SESSION STATS", bg=PANEL, fg=MUTED,
                 font=F_LABEL).pack(anchor="w", padx=14, pady=(14,6))

        self._lbl_done     = self._scard(inner, "✔  Completed", "0",  GREEN)
        self._lbl_err      = self._scard(inner, "✘  Errors",    "0",  RED)
        self._lbl_speed_sb = self._scard(inner, "⚡ Speed",      "—",  BLUE)
        self._lbl_eta_sb   = self._scard(inner, "🕐 ETA",        "—",  YELLOW)

        # ── Save folder
        tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", pady=(8,10), padx=14)
        tk.Label(inner, text="SAVE FOLDER", bg=PANEL, fg=MUTED,
                 font=F_LABEL).pack(anchor="w", padx=14)
        self._folder_lbl = tk.Label(inner, text=self._save_dir, bg=PANEL, fg=MUTED,
                 font=_f(9,"bold"), wraplength=200, justify="left")
        self._folder_lbl.pack(anchor="w", padx=14, pady=(4,6))
        tk.Button(inner, text="📁  Change Folder", bg=CARD2, fg=FG,
                  activebackground=BORDER, activeforeground=WHITE,
                  relief="flat", font=_f(9,"bold"), cursor="hand2",
                  command=self._browse_folder, padx=8, pady=5
                  ).pack(fill="x", padx=14, pady=(0,4))

        # ── Cloud shortcuts
        tk.Frame(inner, bg=BORDER, height=1).pack(fill="x", pady=(10,10), padx=14)
        tk.Label(inner, text="QUICK SAVE TO CLOUD", bg=PANEL, fg=MUTED,
                 font=F_LABEL).pack(anchor="w", padx=14, pady=(0,6))

        od_path = _find_onedrive()
        od_ok   = od_path is not None
        tk.Button(inner,
                  text="☁  OneDrive" + (" ✔" if od_ok else " ✘ Not Found"),
                  bg="#0669b3" if od_ok else CARD2,
                  fg=WHITE, activebackground="#055a9a", activeforeground=WHITE,
                  relief="flat", font=_f(9,"bold"),
                  cursor="hand2" if od_ok else "arrow",
                  command=(lambda p=od_path: self._set_cloud(p, "OneDrive")) if od_ok else None,
                  padx=8, pady=5,
                  state="normal" if od_ok else "disabled"
                  ).pack(fill="x", padx=14, pady=(0,8))

        gd_path = _find_googledrive()
        gd_ok   = gd_path is not None
        tk.Button(inner,
                  text="☁  Google Drive" + (" ✔" if gd_ok else " ✘ Not Found"),
                  bg="#1a73e8" if gd_ok else CARD2,
                  fg=WHITE, activebackground="#1765cc", activeforeground=WHITE,
                  relief="flat", font=_f(9,"bold"),
                  cursor="hand2" if gd_ok else "arrow",
                  command=(lambda p=gd_path: self._set_cloud(p, "Google Drive")) if gd_ok else None,
                  padx=8, pady=5,
                  state="normal" if gd_ok else "disabled"
                  ).pack(fill="x", padx=14, pady=(0,8))

        if not od_ok and not gd_ok:
            tk.Label(inner,
                     text="Install OneDrive or Google Drive\nDesktop to enable cloud saving.",
                     bg=PANEL, fg=MUTED, font=_f(8,"bold"),
                     justify="left", wraplength=200).pack(anchor="w", padx=14, pady=(4,10))

    # ── account helpers ──────────────────────────────────────────────────
    def _get_od_account(self):
        """Return OneDrive account email from registry, or None."""
        try:
            base = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                  r"Software\Microsoft\OneDrive\Accounts")
            count = winreg.QueryInfoKey(base)[0]
            for i in range(count):
                sub_name = winreg.EnumKey(base, i)
                sub = winreg.OpenKey(base, sub_name)
                try:
                    email, _ = winreg.QueryValueEx(sub, "UserEmail")
                    winreg.CloseKey(sub)
                    if email:
                        return email
                except Exception:
                    pass
            winreg.CloseKey(base)
        except Exception:
            pass
        return None

    def _get_gd_account(self):
        """Return Google Drive account token (email hint) from registry, or None."""
        import json
        try:
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Google\DriveFS")
            raw, _ = winreg.QueryValueEx(key, "PerAccountPreferences")
            token, _ = winreg.QueryValueEx(key, "CurrentAccountToken")
            winreg.CloseKey(key)
            # Try to find display name / email in the JSON
            data = json.loads(raw)
            for entry in data.get("per_account_preferences", []):
                if entry.get("key") == token:
                    mp = entry.get("value", {}).get("mount_point_path", "")
                    return f"Account ID: {token[:8]}… (Drive: {mp}:\\)" if mp else f"ID: {token[:8]}…"
        except Exception:
            pass
        return None

    def _set_cloud(self, base_path, label):
        """Dialog: show account, let user pick any subfolder inside their cloud, confirm."""
        hdr_col = "#0669b3" if "OneDrive" in label else "#1a73e8"

        dl = tk.Toplevel(self)
        dl.title(f"Save to {label}")
        dl.configure(bg=BG)
        dl.resizable(True, False)
        dl.grab_set()
        dl.transient(self)
        dl.minsize(480, 0)

        # ── header
        hdr = tk.Frame(dl, bg=hdr_col, height=56)
        hdr.pack(fill="x"); hdr.pack_propagate(False)
        tk.Label(hdr, text=f"☁  Save to {label}", bg=hdr_col, fg=WHITE,
                 font=_f(12,"bold")).place(x=16, rely=.5, anchor="w")

        body = tk.Frame(dl, bg=BG)
        body.pack(fill="both", padx=24, pady=(18,6))

        # ── account info
        if "OneDrive" in label:
            acct = self._get_od_account()
        else:
            acct = self._get_gd_account()
        acct_text = f"Account:  {acct}" if acct else "Account:  (signed-in account)"
        tk.Label(body, text=acct_text, bg=BG, fg=FG,
                 font=_f(10,"bold")).pack(anchor="w", pady=(0,10))

        tk.Frame(body, bg=BORDER, height=1).pack(fill="x", pady=(0,12))

        # ── sync root (read-only info)
        tk.Label(body, text="Cloud sync root:", bg=BG, fg=MUTED,
                 font=_f(9,"bold")).pack(anchor="w")
        tk.Label(body, text=base_path, bg=CARD2, fg=BLUE,
                 font=_f(9,"bold"), wraplength=420, justify="left",
                 padx=8, pady=5).pack(fill="x", pady=(4,14))

        # ── destination picker
        tk.Label(body, text="Choose folder inside your cloud storage:",
                 bg=BG, fg=MUTED, font=_f(9,"bold")).pack(anchor="w")

        dest_var = tk.StringVar(value=os.path.join(base_path, "BRAINIAC Downloads"))
        outside_var = tk.BooleanVar(value=False)  # True when dest is outside cloud root

        dest_row = tk.Frame(body, bg=BG)
        dest_row.pack(fill="x", pady=(4,4))

        dest_lbl = tk.Label(dest_row, textvariable=dest_var,
                            bg=CARD2, fg=GREEN, font=_f(9,"bold"),
                            wraplength=340, justify="left", padx=8, pady=6,
                            anchor="w")
        dest_lbl.pack(side="left", fill="x", expand=True)

        def _browse_cloud():
            chosen = filedialog.askdirectory(
                title=f"Choose folder inside {label}",
                initialdir=dest_var.get() if os.path.isdir(dest_var.get()) else base_path,
                mustexist=False,
                parent=dl,
            )
            if chosen:
                import pathlib
                try:
                    pathlib.PurePath(chosen).relative_to(base_path)
                    inside = True
                except ValueError:
                    inside = False
                dest_var.set(chosen)
                if not inside:
                    warn_lbl.config(
                        text=f"☁  Files will be auto-copied to {label} after each download.",
                        fg=GREEN)
                    outside_var.set(True)
                else:
                    warn_lbl.config(text="✅  Files sync to the cloud automatically.", fg=GREEN)
                    outside_var.set(False)

        tk.Button(dest_row, text="📁  Browse…",
                  bg=CARD2, fg=FG, activebackground=BORDER, activeforeground=WHITE,
                  relief="flat", font=_f(9,"bold"), cursor="hand2",
                  command=_browse_cloud, padx=10, pady=6
                  ).pack(side="left", padx=(8,0))

        warn_lbl = tk.Label(body,
                            text="✅  Files sync to the cloud automatically.",
                            bg=BG, fg=GREEN, font=_f(9,"bold"), justify="left")
        warn_lbl.pack(anchor="w", pady=(6,4))

        tk.Label(body,
                 text="📂  A sub-folder named after each playlist title\n"
                      "     will be created automatically inside the chosen folder.",
                 bg=BG, fg=MUTED, font=_f(9,"bold"), justify="left"
                 ).pack(anchor="w", pady=(4,16))

        # ── buttons
        confirmed = tk.BooleanVar(value=False)

        def _confirm():
            confirmed.set(True)
            dl.destroy()

        btn_row = tk.Frame(dl, bg=BG)
        btn_row.pack(fill="x", padx=24, pady=(0,18))
        tk.Button(btn_row, text="✔  Confirm & Use This Folder",
                  bg=hdr_col, fg=WHITE, activebackground=hdr_col,
                  relief="flat", font=_f(10,"bold"), cursor="hand2",
                  command=_confirm, padx=14, pady=8).pack(side="left")
        tk.Button(btn_row, text="Cancel",
                  bg=CARD2, fg=MUTED, activebackground=BORDER,
                  relief="flat", font=_f(10,"bold"), cursor="hand2",
                  command=dl.destroy, padx=14, pady=8).pack(side="left", padx=(10,0))

        dl.update_idletasks()
        w, h = dl.winfo_reqwidth(), dl.winfo_reqheight()
        x = self.winfo_x() + (self.winfo_width() - w) // 2
        y = self.winfo_y() + (self.winfo_height() - h) // 2
        dl.geometry(f"+{x}+{y}")
        self.wait_window(dl)

        if not confirmed.get():
            return

        dest = dest_var.get()
        try:
            os.makedirs(dest, exist_ok=True)
        except Exception as e:
            messagebox.showerror("Cloud Folder Error",
                                 f"Could not create folder:\n{dest}\n\n{e}")
            return
        self._save_dir = dest
        self._folder_lbl.config(text=self._save_dir)
        # If outside cloud root, set up auto-mirror to the cloud base path
        if outside_var.get():
            self._cloud_mirror = base_path
            self._cloud_label  = label
            self._log(f"☁  Auto-copy to {label} enabled → {base_path}", "info")
        else:
            self._cloud_mirror = None
            self._cloud_label  = ""
        self._log(f"☁  Saving to {label} → {dest}", "info")

    def _browse_folder(self):
        chosen = filedialog.askdirectory(
            title="Choose Download Folder",
            initialdir=self._save_dir,
            mustexist=False,
        )
        if chosen:
            self._save_dir = chosen
            os.makedirs(self._save_dir, exist_ok=True)
            self._folder_lbl.config(text=self._save_dir)
            self._cloud_mirror = None   # regular folder — no auto-mirror
            self._cloud_label  = ""
            self._log(f"📁  Save folder → {self._save_dir}", "info")

    def _scard(self, p, label, val, color):
        c = tk.Frame(p, bg=CARD2, highlightbackground=BORDER, highlightthickness=1)
        c.pack(fill="x", pady=(0,8))
        tk.Label(c, text=label, bg=CARD2, fg=MUTED, font=_f(9,"bold")).pack(anchor="w", padx=10, pady=(6,0))
        lbl = tk.Label(c, text=val, bg=CARD2, fg=color, font=_f(17,"bold"))
        lbl.pack(anchor="w", padx=10, pady=(0,6))
        return lbl

    # ── MAIN ────────────────────────────────────────────────────────────────
    def _build_main(self):
        main = tk.Frame(self, bg=BG)
        main.pack(side="left", fill="both", expand=True)

        tb = tk.Frame(main, bg=PANEL, height=50)
        tb.pack(fill="x"); tb.pack_propagate(False)
        self._head_lbl = tk.Label(tb, text="MUSIC DOWNLOADER",
                                  bg=PANEL, fg=FG, font=F_HEAD)
        self._head_lbl.place(x=20, rely=.5, anchor="w")

        # ── Mode tab buttons (top-right of toolbar)
        _tbf = tk.Frame(tb, bg=PANEL)
        _tbf.place(relx=1.0, rely=0.5, anchor="e", x=-14)
        self._tab_yt = tk.Button(_tbf, text="📺  YouTube Playlist",
                                  bg=CARD2, fg=MUTED, relief="flat",
                                  font=_f(9,"bold"), cursor="hand2", padx=12, pady=6,
                                  activebackground=RED, activeforeground=WHITE,
                                  command=lambda: self._switch_mode("playlist"))
        self._tab_yt.pack(side="left", padx=(0,4))
        self._tab_mu = tk.Button(_tbf, text="🎵  Music Downloader",
                                  bg=RED, fg=WHITE, relief="flat",
                                  font=_f(9,"bold"), cursor="hand2", padx=12, pady=6,
                                  activebackground=RED, activeforeground=WHITE,
                                  command=lambda: self._switch_mode("music"))
        self._tab_mu.pack(side="left", padx=(0,4))
        self._tab_mv = tk.Button(_tbf, text="🎬  Movies",
                                  bg=CARD2, fg=MUTED, relief="flat",
                                  font=_f(9,"bold"), cursor="hand2", padx=12, pady=6,
                                  activebackground=RED, activeforeground=WHITE,
                                  command=lambda: self._switch_mode("movies"))
        self._tab_mv.pack(side="left", padx=(0,4))
        self._tab_dl = tk.Button(_tbf, text="🔗  Direct Download",
                                  bg=CARD2, fg=MUTED, relief="flat",
                                  font=_f(9,"bold"), cursor="hand2", padx=12, pady=6,
                                  activebackground=PURPLE, activeforeground=WHITE,
                                  command=lambda: self._switch_mode("direct"))
        self._tab_dl.pack(side="left", padx=(0,4))

        # Scrollable area
        cvs = tk.Canvas(main, bg=BG, highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(main, orient="vertical", command=cvs.yview)
        cvs.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        cvs.pack(side="left", fill="both", expand=True)

        sf = tk.Frame(cvs, bg=BG)
        wid = cvs.create_window((0,0), window=sf, anchor="nw")
        cvs.bind("<Configure>", lambda e: cvs.itemconfig(wid, width=e.width))
        sf.bind("<Configure>",  lambda e: cvs.configure(scrollregion=cvs.bbox("all")))
        cvs.bind_all("<MouseWheel>", lambda e: cvs.yview_scroll(int(-1*(e.delta/120)), "units"))

        P = dict(padx=22, pady=(0,14))

        # ── URL card (YouTube Playlist) ──────────────────────────────────────
        self._url_section_lbl = tk.Label(sf, text="YOUTUBE PLAYLIST URL",
                                          bg=BG, fg=MUTED, font=F_LABEL)
        self._url_card = tk.Frame(sf, bg=CARD,
                                   highlightbackground=BORDER, highlightthickness=1)
        self._sites_lbl = tk.Label(
            self._url_card,
            text="Paste a YouTube playlist URL to download every video in the playlist.",
            bg=CARD, fg=MUTED, font=_f(8,"bold"),
            anchor="w", wraplength=680, justify="left")
        self._sites_lbl.pack(fill="x", padx=14, pady=(10,0))
        urow = tk.Frame(self._url_card, bg=CARD)
        urow.pack(fill="x", padx=14, pady=14)
        self.url_var = tk.StringVar()
        self.url_entry = tk.Entry(
            urow, textvariable=self.url_var,
            bg="#0d1320", fg=FG, insertbackground=FG,
            relief="flat", font=F_BODY,
            highlightbackground=BORDER, highlightthickness=1,
            highlightcolor=RED)
        self.url_entry.pack(side="left", fill="x", expand=True, ipady=11, padx=(0,12))
        self.url_entry.bind("<Return>",       lambda e: self._start())
        self.url_entry.bind("<Control-v>",    self._paste)
        self.url_entry.bind("<Control-V>",    self._paste)
        self.url_entry.bind("<Shift-Insert>", self._paste)
        _url_ctx = tk.Menu(self, tearoff=0, bg=CARD2, fg=FG,
                           activebackground=RED, activeforeground=WHITE, relief="flat")
        _url_ctx.add_command(label="Paste",      command=self._paste)
        _url_ctx.add_command(label="Cut",        command=lambda: self.url_entry.event_generate("<<Cut>>"))
        _url_ctx.add_command(label="Copy",       command=lambda: self.url_entry.event_generate("<<Copy>>"))
        _url_ctx.add_separator()
        _url_ctx.add_command(label="Select All", command=lambda: self.url_entry.select_range(0, "end"))
        _url_ctx.add_command(label="Clear",      command=lambda: self.url_var.set(""))
        self.url_entry.bind("<Button-3>", lambda e: _url_ctx.tk_popup(e.x_root, e.y_root))
        self.btn = tk.Button(
            urow, text="  ⬇  Start Download",
            bg=RED, fg=WHITE, activebackground=RED2,
            activeforeground=WHITE, relief="flat",
            font=_f(11,"bold"), cursor="hand2",
            command=self._start, padx=18)
        self.btn.pack(side="right", ipady=11)
        # Quality row
        qrow = tk.Frame(self._url_card, bg=CARD)
        qrow.pack(fill="x", padx=14, pady=(0,14))
        tk.Label(qrow, text="VIDEO QUALITY:", bg=CARD, fg=MUTED,
                 font=F_LABEL).pack(side="left", padx=(0,12))
        QUALITIES = ["Best Quality", "1080p", "720p", "480p", "360p", "Audio Only (MP3)"]
        sty2 = ttk.Style(self)
        sty2.configure("Q.TCombobox",
                        fieldbackground="#0d1320", background=CARD2,
                        foreground=FG, arrowcolor=FG,
                        selectbackground=CARD2, selectforeground=FG)
        self.quality_cb = ttk.Combobox(
            qrow, textvariable=self.quality_var,
            values=QUALITIES, state="readonly",
            font=F_BODY, style="Q.TCombobox", width=22)
        self.quality_cb.pack(side="left")
        # hidden initially (music mode is default)
        self._url_section_lbl.pack_forget()
        self._url_card.pack_forget()

        # ── Music/Movies Search card (shown in music & movies modes)
        self._search_sec_lbl = tk.Label(sf, text="SEARCH MUSIC", bg=BG, fg=MUTED, font=F_LABEL)
        self._search_card = tk.Frame(sf, bg=CARD, highlightbackground=BORDER, highlightthickness=1)

        scard_top = tk.Frame(self._search_card, bg=CARD)
        scard_top.pack(fill="x", padx=14, pady=14)

        self._search_var = tk.StringVar()
        self._search_entry = tk.Entry(
            scard_top, textvariable=self._search_var,
            bg="#0d1320", fg=FG, insertbackground=FG,
            relief="flat", font=F_BODY,
            highlightbackground=BORDER, highlightthickness=1,
            highlightcolor=BLUE)
        self._search_entry.pack(side="left", fill="x", expand=True, ipady=11, padx=(0,10))
        self._search_entry.bind("<Return>", lambda e: self._search_music())

        self._search_btn = tk.Button(
            scard_top, text="  🔍  Search",
            bg=BLUE, fg="#0a0e14", activebackground="#74c0fc",
            activeforeground="#0a0e14", relief="flat",
            font=_f(11,"bold"), cursor="hand2",
            command=self._search_music, padx=14)
        self._search_btn.pack(side="right", ipady=11)

        # Source selector — YouTube / SoundCloud / Dailymotion (always visible)
        src_row = tk.Frame(self._search_card, bg=CARD)
        src_row.pack(fill="x", padx=14, pady=(0,6))
        self._search_lbl_var = tk.StringVar(value="Search on:")
        self._src_row_lbl = tk.Label(src_row, textvariable=self._search_lbl_var,
                                     bg=CARD, fg=MUTED, font=F_LABEL)
        self._src_row_lbl.pack(side="left", padx=(0,10))
        self._search_src = tk.StringVar(value="YouTube")
        for _src, _col in (("YouTube", FG), ("SoundCloud", FG), ("Dailymotion", FG)):
            tk.Radiobutton(src_row, text=_src, variable=self._search_src, value=_src,
                           bg=CARD, fg=_col, selectcolor=CARD2,
                           activebackground=CARD, activeforeground=FG,
                           font=_f(9,"bold"), cursor="hand2"
                           ).pack(side="left", padx=(0,14))

        # Results area — fixed-height scrollable sub-canvas
        self._res_wrapper = tk.Frame(self._search_card, bg=CARD)
        # (packed/forgotten dynamically in _show_search_results)
        res_cvs_container = tk.Frame(self._res_wrapper, bg=CARD)
        res_cvs_container.pack(fill="x", padx=0, pady=0)
        self._res_cvs = tk.Canvas(res_cvs_container, bg=CARD,
                                   highlightthickness=0, height=320)
        res_vsb = ttk.Scrollbar(res_cvs_container, orient="vertical",
                                command=self._res_cvs.yview)
        self._res_cvs.configure(yscrollcommand=res_vsb.set)
        res_vsb.pack(side="right", fill="y")
        self._res_cvs.pack(side="left", fill="both", expand=True)
        self._results_outer = tk.Frame(self._res_cvs, bg=CARD)
        self._res_win = self._res_cvs.create_window((0, 0), window=self._results_outer, anchor="nw")
        self._res_cvs.bind("<Configure>",
            lambda e: self._res_cvs.itemconfig(self._res_win, width=e.width))
        self._results_outer.bind("<Configure>",
            lambda e: self._res_cvs.configure(scrollregion=self._res_cvs.bbox("all")))
        self._res_cvs.bind_all("<MouseWheel>",
            lambda e: self._res_cvs.yview_scroll(int(-1*(e.delta/120)), "units")
            if self._res_wrapper.winfo_ismapped() else None)
        self._result_vars   = []   # list of (BooleanVar, url, title, chk_lbl)
        self._result_frames = []

        # action row below results
        self._res_action_row = tk.Frame(self._search_card, bg=CARD)
        self._res_action_row.pack(fill="x", padx=14, pady=(0,12))
        tk.Button(self._res_action_row, text="☑  Select All",
                  bg=CARD2, fg=MUTED, relief="flat", font=_f(9,"bold"),
                  cursor="hand2", padx=8,
                  command=lambda: [
                      (v.set(True), lbl.config(text="☑", fg=GREEN))
                      for v, _, __, lbl in self._result_vars]
                  ).pack(side="left", padx=(0,6))
        tk.Button(self._res_action_row, text="☐  Clear All",
                  bg=CARD2, fg=MUTED, relief="flat", font=_f(9,"bold"),
                  cursor="hand2", padx=8,
                  command=lambda: [
                      (v.set(False), lbl.config(text="☐", fg=MUTED))
                      for v, _, __, lbl in self._result_vars]
                  ).pack(side="left")
        self._dl_sel_btn = tk.Button(
            self._res_action_row, text="  ⬇  Download Selected",
            bg=RED, fg=WHITE, activebackground=RED2, activeforeground=WHITE,
            relief="flat", font=_f(11,"bold"), cursor="hand2", padx=14,
            command=self._download_selected_music)
        self._dl_sel_btn.pack(side="right")
        self._res_action_row.pack_forget()   # hidden until results arrive
        self._res_wrapper.pack_forget()       # hidden until results arrive

        # hide search card initially (starts in Music mode — will be shown by _switch_mode)
        self._search_sec_lbl.pack_forget()
        self._search_card.pack_forget()

        # ── Direct Download card ──────────────────────────────────────────────
        self._dl_sec_lbl = tk.Label(sf, text="DIRECT FILE DOWNLOAD", bg=BG, fg=MUTED,
                                     font=F_LABEL)
        self._dl_card = tk.Frame(sf, bg=CARD,
                                  highlightbackground=BORDER, highlightthickness=1)
        # URL input row
        dl_top = tk.Frame(self._dl_card, bg=CARD)
        dl_top.pack(fill="x", padx=14, pady=14)
        self._dl_url_var = tk.StringVar()
        self._dl_url_entry = tk.Entry(
            dl_top, textvariable=self._dl_url_var,
            bg="#0d1320", fg=FG, insertbackground=FG,
            relief="flat", font=F_BODY,
            highlightbackground=BORDER, highlightthickness=1,
            highlightcolor=PURPLE)
        self._dl_url_entry.pack(side="left", fill="x", expand=True, ipady=11, padx=(0,10))
        self._dl_url_entry.bind("<Return>",       lambda e: self._fetch_url_info())
        self._dl_url_entry.bind("<Control-v>",    self._dl_paste)
        self._dl_url_entry.bind("<Control-V>",    self._dl_paste)
        self._dl_url_entry.bind("<Shift-Insert>", self._dl_paste)

        dl_ctx = tk.Menu(self, tearoff=0, bg=CARD2, fg=FG,
                         activebackground=PURPLE, activeforeground=WHITE, relief="flat")
        dl_ctx.add_command(label="Paste",      command=self._dl_paste)
        dl_ctx.add_command(label="Cut",        command=lambda: self._dl_url_entry.event_generate("<<Cut>>"))
        dl_ctx.add_command(label="Copy",       command=lambda: self._dl_url_entry.event_generate("<<Copy>>"))
        dl_ctx.add_separator()
        dl_ctx.add_command(label="Select All", command=lambda: self._dl_url_entry.select_range(0, "end"))
        dl_ctx.add_command(label="Clear",      command=lambda: self._dl_url_var.set(""))
        self._dl_url_entry.bind("<Button-3>",
                                lambda e: dl_ctx.tk_popup(e.x_root, e.y_root))

        self._dl_fetch_btn = tk.Button(
            dl_top, text="  🔍  Fetch Info",
            bg=PURPLE, fg=WHITE, activebackground="#7c5cbf",
            activeforeground=WHITE, relief="flat",
            font=_f(11,"bold"), cursor="hand2",
            command=self._fetch_url_info, padx=14)
        self._dl_fetch_btn.pack(side="right", ipady=11)
        # hint label
        tk.Label(self._dl_card,
                 text="Paste any URL — direct file link, YouTube, SoundCloud, TikTok, Vimeo, " 
                      "Instagram, Twitter/X, Dailymotion, or any yt-dlp supported site.",
                 bg=CARD, fg=MUTED, font=_f(8,"bold"),
                 wraplength=700, justify="left"
                 ).pack(anchor="w", padx=14, pady=(0,10))
        # Preview section — dynamically rebuilt on each fetch
        self._dl_preview_frame = tk.Frame(self._dl_card, bg=CARD)
        self._dl_preview_frame.pack(fill="x", padx=14, pady=(0,14))
        # hide until a fetch reveals it
        self._dl_sec_lbl.pack_forget()
        self._dl_card.pack_forget()

        tk.Label(sf, text="DOWNLOAD PROGRESS", bg=BG, fg=MUTED,
                 font=F_LABEL).pack(anchor="w", padx=22, pady=(4,4))
        pcard = tk.Frame(sf, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        pcard.pack(fill="x", **P)
        pin = tk.Frame(pcard, bg=CARD)
        pin.pack(fill="x", padx=16, pady=14)

        self.file_label = tk.Label(pin, text="Waiting for download…",
                                   bg=CARD, fg=MUTED, font=F_BODY, anchor="w")
        self.file_label.pack(fill="x", pady=(0,10))

        sty = ttk.Style(self); sty.theme_use("clam")
        sty.configure("YT.Horizontal.TProgressbar",
                      troughcolor="#0d1320", background=RED,
                      bordercolor=CARD, lightcolor=RED, darkcolor=RED2)
        self.pbar = ttk.Progressbar(pin, style="YT.Horizontal.TProgressbar",
                                    mode="determinate", maximum=100)
        self.pbar.pack(fill="x", pady=(0,12))

        srow = tk.Frame(pin, bg=CARD)
        srow.pack(fill="x")
        for i in range(3): srow.columnconfigure(i, weight=1)

        self._pct_var   = tk.StringVar(value="0 %")
        self._speed_var = tk.StringVar(value="—")
        self._eta_var   = tk.StringVar(value="ETA —")

        for col, (icon, var, col_) in enumerate([
            ("◉  Progress", self._pct_var,   FG),
            ("⚡ Speed",    self._speed_var,  BLUE),
            ("🕐 ETA",      self._eta_var,    YELLOW),
        ]):
            fc = tk.Frame(srow, bg=CARD2, highlightbackground=BORDER, highlightthickness=1)
            fc.grid(row=0, column=col, sticky="ew", padx=(0, 8 if col < 2 else 0))
            tk.Label(fc, text=icon, bg=CARD2, fg=MUTED, font=_f(9,"bold")).pack(anchor="w", padx=10, pady=(7,0))
            tk.Label(fc, textvariable=var, bg=CARD2, fg=col_,
                     font=_f(15,"bold")).pack(anchor="w", padx=10, pady=(0,7))

        self.status_var = tk.StringVar(value="Ready to download")

        sbar = tk.Frame(pcard, bg=CARD)
        sbar.pack(fill="x", padx=16, pady=(0,12))
        tk.Label(sbar, textvariable=self.status_var,
                 bg=CARD, fg=MUTED, font=F_SMALL).pack(side="left")
        self.pause_btn = tk.Button(sbar, text="⏸  Pause",
                                   bg="#2a3a52", fg=FG,
                                   activebackground="#1e2d42", activeforeground=WHITE,
                                   relief="flat", font=_f(9,"bold"),
                                   cursor="hand2", command=self._toggle_pause,
                                   padx=12, pady=4, state="disabled")
        self.pause_btn.pack(side="right")

        # ── Log card
        lhdr = tk.Frame(sf, bg=BG)
        lhdr.pack(fill="x", padx=22, pady=(4,4))
        tk.Label(lhdr, text="ACTIVITY LOG", bg=BG, fg=MUTED, font=F_LABEL).pack(side="left")
        tk.Button(lhdr, text="Clear", bg=CARD2, fg=MUTED, relief="flat",
                  font=_f(9,"bold"), cursor="hand2", command=self._clear_log,
                  padx=8, pady=2).pack(side="right")

        lcard = tk.Frame(sf, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        lcard.pack(fill="both", expand=True, padx=22, pady=(0,22))

        self.log = tk.Text(lcard, bg="#0a0e14", fg=MUTED, font=F_LOG,
                           relief="flat", wrap="word", state="disabled",
                           height=14, padx=10, pady=8,
                           selectbackground=CARD2, selectforeground=FG)
        lvsb = ttk.Scrollbar(lcard, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lvsb.set)
        lvsb.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

        self.log.tag_config("ok",   foreground=GREEN)
        self.log.tag_config("err",  foreground="#f85149")
        self.log.tag_config("info", foreground=BLUE)
        self.log.tag_config("warn", foreground=YELLOW)
        self.log.tag_config("norm", foreground=MUTED)
        self.log.tag_config("head", foreground=PURPLE)

    # ── helpers ─────────────────────────────────────────────────────────────
    @staticmethod
    def _fmt_size(b):
        """Format bytes to human-readable string."""
        if not b:
            return "?"
        for unit in ("B","KB","MB","GB"):
            if b < 1024:
                return f"{b:.1f} {unit}"
            b /= 1024
        return f"{b:.1f} GB"

    def _est_size(self, dur_secs):
        """Estimate file size from duration + current quality setting."""
        if not dur_secs:
            return None
        BPS = {
            "Audio Only (MP3)":   192_000,
            "360p":               500_000,
            "480p":             1_000_000,
            "720p":             2_500_000,
            "1080p":            5_000_000,
            "Best Quality":     5_000_000,
        }
        bps = BPS.get(self.quality_var.get(), 2_500_000)
        return int(dur_secs * bps / 8)

    def _clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0","end")
        self.log.configure(state="disabled")

    def _log(self, msg, tag="norm"):
        def _w():
            self.log.configure(state="normal")
            self.log.insert("end", msg.strip()+"\n", tag)
            self.log.see("end")
            self.log.configure(state="disabled")
        self.after(0, _w)

    def _paste(self, event=None):
        try:
            t = self.clipboard_get()
            self.url_entry.delete(0, "end")
            self.url_entry.insert(0, t.strip())
        except tk.TclError:
            pass
        return "break"

    def _dl_paste(self, event=None):
        try:
            t = self.clipboard_get()
            self._dl_url_entry.delete(0, "end")
            self._dl_url_entry.insert(0, t.strip())
        except tk.TclError:
            pass
        return "break"

    def _reset(self):
        self.pbar["value"] = 0
        self._pct_var.set("0 %")
        self._speed_var.set("—")
        self._eta_var.set("ETA —")
        self.file_label.config(text="Waiting for download…", fg=MUTED)

    def _set_progress(self, pct, pct_str, speed, eta):
        def _u():
            self.pbar["value"] = pct
            self._pct_var.set(pct_str or f"{pct:.1f}%")
            self._speed_var.set(speed or "—")
            self._eta_var.set(f"ETA {eta}" if eta else "ETA —")
            self._lbl_speed_sb.config(text=speed or "—")
            self._lbl_eta_sb.config(text=eta or "—")
        self.after(0, _u)

    def _toggle_pause(self):
        if not self._is_downloading:
            return
        if self._paused:
            self._paused = False
            self._pause_evt.set()
            self.pause_btn.config(text="⏸  Pause", bg="#2a3a52")
            self._log("▶  Resumed.", "info")
            self.after(0, lambda: self.status_var.set("Downloading…"))
        else:
            self._paused = True
            self._pause_evt.clear()
            self.pause_btn.config(text="▶  Resume", bg=GREEN)
            self._log("⏸  Paused — waiting between fragments…", "warn")
            self.after(0, lambda: self.status_var.set("⏸  Paused"))

    def _set_btn(self, on):
        def _b():
            if on:
                if self._mode == "playlist":
                    self.btn.config(text="  ⬇  Start Download", bg=RED, state="normal")
                self.pause_btn.config(text="⏸  Pause", bg="#2a3a52", state="disabled")
                self._paused = False
                self._pause_evt.set()
            else:
                if self._mode == "playlist":
                    self.btn.config(text="  ⏳  Downloading…", bg="#5a0e14", state="disabled")
                self.pause_btn.config(state="normal")
        self.after(0, _b)
    # ── Mode switch ───────────────────────────────────────────────────
    def _switch_mode(self, mode):
        self._mode = mode
        # reset all tab button colours
        for btn in (self._tab_yt, self._tab_mu, self._tab_mv, self._tab_dl):
            btn.config(bg=CARD2, fg=MUTED)

        if mode == "playlist":
            self.title("BRAINIAC YOUTUBE PLAYLIST DOWNLOADER")
            self._head_lbl.config(text="YOUTUBE PLAYLIST DOWNLOADER")
            self._tab_yt.config(bg=RED, fg=WHITE)
            self.quality_var.set("Best Quality")
            self._search_sec_lbl.pack_forget()
            self._search_card.pack_forget()
            self._dl_sec_lbl.pack_forget()
            self._dl_card.pack_forget()
            self._url_section_lbl.pack(anchor="w", padx=22, pady=(18,4))
            self._url_card.pack(fill="x", padx=22, pady=(0,14))
            self.after(100, self.url_entry.focus_set)

        elif mode == "music":
            self.title("BRAINIAC MUSIC DOWNLOADER")
            self._head_lbl.config(text="MUSIC DOWNLOADER")
            self._tab_mu.config(bg=RED, fg=WHITE)
            self._url_section_lbl.pack_forget()
            self._url_card.pack_forget()
            self.quality_var.set("Audio Only (MP3)")
            self._dl_sec_lbl.pack_forget()
            self._dl_card.pack_forget()
            self._search_sec_lbl.config(text="SEARCH MUSIC")
            self._search_src.set("YouTube")
            self._search_entry.config(highlightcolor=BLUE)
            self._search_btn.config(bg=BLUE, fg="#0a0e14")
            self._search_sec_lbl.pack(anchor="w", padx=22, pady=(18,4))
            self._search_card.pack(fill="x", padx=22, pady=(0,14))
            self.after(100, self._search_entry.focus_set)

        elif mode == "movies":
            self.title("BRAINIAC MOVIES DOWNLOADER")
            self._head_lbl.config(text="MOVIES DOWNLOADER")
            self._tab_mv.config(bg="#e67700", fg=WHITE)
            self._url_section_lbl.pack_forget()
            self._url_card.pack_forget()
            self.quality_var.set("Best Quality")
            self._dl_sec_lbl.pack_forget()
            self._dl_card.pack_forget()
            self._search_sec_lbl.config(text="SEARCH MOVIES")
            self._search_src.set("YouTube")
            self._search_entry.config(highlightcolor="#e67700")
            self._search_btn.config(bg="#e67700", fg=WHITE)
            self._search_sec_lbl.pack(anchor="w", padx=22, pady=(18,4))
            self._search_card.pack(fill="x", padx=22, pady=(0,14))
            self.after(100, self._search_entry.focus_set)

        elif mode == "direct":
            self.title("BRAINIAC DIRECT FILE DOWNLOADER")
            self._head_lbl.config(text="DIRECT FILE DOWNLOADER")
            self._tab_dl.config(bg=PURPLE, fg=WHITE)
            self._url_section_lbl.pack_forget()
            self._url_card.pack_forget()
            self._search_sec_lbl.pack_forget()
            self._search_card.pack_forget()
            self._dl_sec_lbl.pack(anchor="w", padx=22, pady=(18,4))
            self._dl_card.pack(fill="x", padx=22, pady=(0,14))
            self.after(100, self._dl_url_entry.focus_set)


    # ── Music Search ────────────────────────────────────────────────
    def _search_music(self):
        query = self._search_var.get().strip()
        if not query:
            self._log("⚠  Type a song name to search.", "warn")
            return
        self._search_btn.config(state="disabled", text="  ⏳  Searching…")
        self._log(f"🔍  Searching for: {query}", "info")
        threading.Thread(target=self._do_search, args=(query,), daemon=True).start()

    def _do_search(self, query):
        src  = self._search_src.get()
        mode = self._mode
        suffix = " full movie" if mode == "movies" else ""

        PREFIXES = {
            "YouTube":     "ytsearch20",
            "SoundCloud":  "scsearch20",
            "Dailymotion": "dtsearch20",
        }
        prefix     = PREFIXES.get(src, "ytsearch20")
        search_url = f"{prefix}:{query}{suffix}"
        results = []
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True,
                                    "ignoreerrors": True}) as ydl:
                info = ydl.extract_info(search_url, download=False)
                if info and "entries" in info:
                    for e in info["entries"]:
                        if not e:
                            continue
                        dur = e.get("duration") or 0
                        m, s = divmod(int(dur), 60)
                        raw_size = e.get("filesize") or e.get("filesize_approx") or 0
                        results.append({
                            "title":    e.get("title", "Unknown"),
                            "channel":  e.get("uploader") or e.get("channel") or "",
                            "duration": f"{m}:{s:02d}" if dur else "?",
                            "dur_secs": dur,
                            "size_bytes": raw_size,
                            "url":      e.get("url") or e.get("webpage_url", ""),
                        })
        except Exception as ex:
            self._log(f"⚠  Search error: {ex}", "warn")
        self.after(0, lambda: self._show_search_results(results))

    def _show_search_results(self, results):
        self._search_btn.config(state="normal", text="  🔍  Search")
        # clear previous results
        for w in self._result_frames:
            w.destroy()
        self._result_frames.clear()
        self._result_vars.clear()
        self._res_wrapper.pack_forget()
        self._res_action_row.pack_forget()

        if not results:
            self._log("⚠  No results found. Try a different query.", "warn")
            self._res_wrapper.pack_forget()
            self._res_action_row.pack_forget()
            return

        self._log(f"✅  Found {len(results)} result(s). Select songs to download.", "ok")

        # show results wrapper inside the search card
        self._res_wrapper.pack(fill="x", padx=14, pady=(0,6))

        for i, r in enumerate(results):
            var = tk.BooleanVar(value=False)

            rbg = CARD2 if i % 2 == 0 else CARD
            row = tk.Frame(self._results_outer, bg=rbg,
                           highlightbackground=BORDER, highlightthickness=1)
            row.pack(fill="x", pady=(0,2))
            self._result_frames.append(row)

            # Custom visual checkbox label ☐ / ☑
            chk_lbl = tk.Label(row, text="☐", bg=rbg, fg=MUTED,
                               font=_f(14,"bold"), cursor="hand2", width=2)
            chk_lbl.pack(side="left", padx=(8,0), pady=4)

            self._result_vars.append((var, r["url"], r["title"], chk_lbl))

            def _toggle(event=None, v=var, lbl=chk_lbl):
                v.set(not v.get())
                lbl.config(text="☑" if v.get() else "☐",
                           fg=GREEN if v.get() else MUTED)

            chk_lbl.bind("<Button-1>", _toggle)
            row.bind("<Button-1>", _toggle)

            tk.Label(row, text=f"{i+1:>2}.", bg=rbg, fg=MUTED,
                     font=_f(9,"bold"), width=3).pack(side="left")
            title_lbl = tk.Label(row, text=r["title"], bg=rbg, fg=FG,
                     font=_f(10,"bold"), anchor="w")
            title_lbl.pack(side="left", fill="x", expand=True, padx=(4,8))
            title_lbl.bind("<Button-1>", _toggle)
            # size column
            real_sz  = r.get("size_bytes") or 0
            est_sz   = self._est_size(r.get("dur_secs") or 0)
            if real_sz:
                size_txt = self._fmt_size(real_sz)
                size_fg  = GREEN
            elif est_sz:
                size_txt = f"~{self._fmt_size(est_sz)}"
                size_fg  = YELLOW
            else:
                size_txt = "?"
                size_fg  = MUTED
            tk.Label(row, text=size_txt, bg=rbg, fg=size_fg,
                     font=_f(9,"bold"), width=9).pack(side="right", padx=(0,4))
            tk.Label(row, text=r["duration"], bg=rbg, fg=BLUE,
                     font=_f(9,"bold"), width=6).pack(side="right", padx=(0,4))
            tk.Label(row, text=r["channel"], bg=rbg, fg=MUTED,
                     font=_f(9,"bold")).pack(side="right", padx=(0,10))

        self._res_action_row.pack(fill="x", padx=14, pady=(6,12))

        # scroll back to top of results
        self._res_cvs.yview_moveto(0)

    def _download_selected_music(self):
        selected = [(url, title) for var, url, title, _ in self._result_vars if var.get()]
        if not selected:
            self._log("⚠  Tick at least one song to download.", "warn")
            return
        if self._is_downloading:
            self._log("⚠  A download is already in progress.", "warn")
            return
        self._log(f"📥  Queuing {len(selected)} song(s)…", "info")
        threading.Thread(target=self._download_music_list,
                         args=(selected,), daemon=True).start()

    def _download_music_list(self, items):
        """Download a list of (url, title) tracks one by one."""
        self._is_downloading = True
        self.after(0, lambda: self._set_btn(False))
        for url, title in items:
            self._log(f"🎵  {title}", "head")
            self.after(0, lambda t=title: self.status_var.set(f"Downloading: {t}"))
            self._pause_evt.wait()
            self._download(url, _batch=True)
        self._is_downloading = False
        self.after(0, lambda: self._set_btn(True))
        self.after(0, lambda: self.status_var.set("All done!"))
        self.after(0, lambda: self._lbl_speed_sb.config(text="—"))
        self.after(0, lambda: self._lbl_eta_sb.config(text="—"))
        self._log(f"🎉  Finished {len(items)} track(s).", "ok")
    # ── Direct URL Download ──────────────────────────────────────────────────
    def _fetch_url_info(self):
        """Validate URL and kick off background probe."""
        url = self._dl_url_var.get().strip()
        if not url:
            self._log("⚠  Paste a URL first.", "warn")
            return
        if not url.startswith(("http://", "https://")):
            self._log("⚠  URL must start with http:// or https://", "warn")
            return
        # clear old preview
        for w in self._dl_preview_frame.winfo_children():
            w.destroy()
        self._dl_fetch_btn.config(state="disabled", text="  ⏳  Fetching…")
        self._log(f"🔍  Probing URL: {url}", "info")
        threading.Thread(target=self._do_fetch_url, args=(url,), daemon=True).start()

    def _do_fetch_url(self, url):
        """Background: HEAD request + yt-dlp extract_info probe."""
        import urllib.request, urllib.error, re, mimetypes
        meta = {
            "url": url, "filename": None, "size": None,
            "mime": None, "ext": None, "is_direct": False,
            "title": None, "uploader": None, "duration": None,
            "thumbnail": None, "description": None,
            "error": None, "ydlp_ok": False,
        }

        # ── Step 1: HEAD request ────────────────────────────────────────────
        try:
            req = urllib.request.Request(
                url, method="HEAD",
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            with urllib.request.urlopen(req, timeout=12) as resp:
                ct  = resp.headers.get("Content-Type", "")
                cl  = resp.headers.get("Content-Length", "")
                cd  = resp.headers.get("Content-Disposition", "")
                meta["mime"] = ct.split(";")[0].strip() or "application/octet-stream"
                if cl:
                    try: meta["size"] = int(cl)
                    except: pass
                # filename from Content-Disposition
                m = re.search(r'filename[^;=\n]*=(["\']?)([^"\';\n]+)\1', cd)
                if m:
                    meta["filename"] = m.group(2).strip()
                HTML_TYPES = ("text/html", "text/xhtml", "text/xml",
                              "application/xhtml", "application/xml")
                is_html = any(t in meta["mime"] for t in HTML_TYPES)
                meta["is_direct"] = not is_html
        except Exception as e:
            meta["error"] = str(e)

        # filename fallback from URL path
        if not meta["filename"]:
            path = url.split("?")[0].split("#")[0]
            base = path.rstrip("/").split("/")[-1]
            if "." in base:
                meta["filename"] = base

        # extension detection
        if meta["filename"] and "." in meta["filename"]:
            meta["ext"] = meta["filename"].rsplit(".", 1)[-1].lower()
        elif meta["mime"]:
            import mimetypes as _mt
            guessed = _mt.guess_extension(meta["mime"])
            if guessed:
                meta["ext"] = guessed.lstrip(".")

        # ── Step 2: yt-dlp rich metadata probe ─────────────────────────────
        try:
            with yt_dlp.YoutubeDL({
                "quiet": True, "extract_flat": False,
                "ignoreerrors": True, "skip_download": True,
                "noplaylist": True, "socket_timeout": 15,
            }) as ydl:
                info = ydl.extract_info(url, download=False)
                if info and info.get("title"):
                    meta["ydlp_ok"]   = True
                    # keep is_direct=True if HEAD already confirmed it's a raw file
                    # only mark non-direct for real media pages
                    if not meta["is_direct"]:
                        meta["is_direct"] = False
                    meta["title"]      = info.get("title", "")
                    meta["uploader"]   = info.get("uploader") or info.get("channel") or ""
                    meta["thumbnail"]  = info.get("thumbnail") or ""
                    dur = info.get("duration") or 0
                    meta["duration"]   = dur
                    meta["description"] = (info.get("description") or "")[:300]
                    if not meta["size"]:
                        meta["size"] = info.get("filesize") or info.get("filesize_approx") or None
                    if not meta["ext"] or meta["ext"] == "html":
                        meta["ext"] = info.get("ext") or "mp4"
                    if not meta["filename"]:
                        import re as _re
                        safe = _re.sub(r'[<>:"/\\|?*]', '_', meta["title"])
                        meta["filename"] = f"{safe}.{meta['ext']}"
        except Exception:
            pass

        # final filename fallback
        if not meta["filename"]:
            meta["filename"] = url.split("?")[0].rstrip("/").split("/")[-1] or "download"

        self.after(0, lambda m=meta: self._show_url_preview(m))

    def _show_url_preview(self, meta):
        """Render file preview panel inside _dl_preview_frame."""
        self._dl_fetch_btn.config(state="normal", text="  🔍  Fetch Info")

        frame = self._dl_preview_frame
        for w in frame.winfo_children():
            w.destroy()

        # ── Nothing downloadable found ──────────────────────────────────────
        if not meta.get("is_direct") and not meta.get("ydlp_ok"):
            sep = tk.Frame(frame, bg=BORDER, height=1)
            sep.pack(fill="x", pady=(4, 10))
            msg_frame = tk.Frame(frame, bg="#1a0a0a",
                                 highlightbackground=RED, highlightthickness=1)
            msg_frame.pack(fill="x", pady=(0, 8))
            tk.Label(msg_frame,
                     text="\u274c  Not a downloadable file",
                     bg="#1a0a0a", fg=RED, font=_f(11, "bold")
                     ).pack(anchor="w", padx=12, pady=(10, 2))
            hint = ("This URL points to a webpage, not a file.\n"
                    "\u2022 For YouTube / SoundCloud / TikTok / Vimeo etc. "
                    "use the Music, Movies or YouTube Playlist tabs.\n"
                    "\u2022 For direct files use a link ending in "
                    ".mp4  .mp3  .pdf  .zip  .jpg  .exe \u2026")
            tk.Label(msg_frame, text=hint,
                     bg="#1a0a0a", fg=MUTED, font=_f(9, "bold"),
                     justify="left", wraplength=580
                     ).pack(anchor="w", padx=12, pady=(0, 10))
            if meta.get("error"):
                tk.Label(frame, text=f"Detail: {meta['error']}",
                         bg=CARD, fg=MUTED, font=_f(8, "bold"),
                         wraplength=580, justify="left").pack(anchor="w", padx=2)
            self._log("\u26a0  URL does not point to a downloadable file.", "warn")
            return

        if meta.get("error") and not meta.get("ydlp_ok"):
            tk.Label(frame, text=f"\u26a0  Could not probe URL: {meta['error']}",
                     bg=CARD, fg=YELLOW, font=_f(9,"bold")).pack(anchor="w", pady=4)

        # ── file type icon map ──────────────────────────────────────────────
        EXT_ICON = {
            "mp4":"🎬","mkv":"🎬","avi":"🎬","mov":"🎬","webm":"🎬","flv":"🎬",
            "mp3":"🎵","m4a":"🎵","ogg":"🎵","flac":"🎵","wav":"🎵","aac":"🎵",
            "jpg":"🖼","jpeg":"🖼","png":"🖼","gif":"🖼","webp":"🖼","bmp":"🖼","svg":"🖼",
            "pdf":"📄","doc":"📄","docx":"📄","xls":"📊","xlsx":"📊","pptx":"📊",
            "zip":"🗜","rar":"🗜","7z":"🗜","tar":"🗜","gz":"🗜",
            "exe":"⚙","msi":"⚙","apk":"📱",
        }
        ext  = (meta.get("ext") or "").lower()
        icon = EXT_ICON.get(ext, "📁")
        mime = meta.get("mime") or "unknown"

        # ── top info bar ───────────────────────────────────────────────────
        sep = tk.Frame(frame, bg=BORDER, height=1)
        sep.pack(fill="x", pady=(4,10))

        info_row = tk.Frame(frame, bg=CARD)
        info_row.pack(fill="x")

        tk.Label(info_row, text=icon, bg=CARD, fg=FG,
                 font=_f(28,"bold")).pack(side="left", padx=(0,12))

        details = tk.Frame(info_row, bg=CARD)
        details.pack(side="left", fill="x", expand=True)

        title_txt = meta.get("title") or meta.get("filename") or "Unknown file"
        tk.Label(details, text=title_txt, bg=CARD, fg=WHITE,
                 font=_f(11,"bold"), anchor="w", wraplength=520,
                 justify="left").pack(anchor="w")

        if meta.get("uploader"):
            tk.Label(details, text=f"by  {meta['uploader']}",
                     bg=CARD, fg=MUTED, font=_f(9,"bold")).pack(anchor="w")

        meta_row = tk.Frame(details, bg=CARD)
        meta_row.pack(anchor="w", pady=(4,0))

        # size chip
        size_txt = self._fmt_size(meta["size"]) if meta.get("size") else "Unknown size"
        for chip_txt, chip_col in [
            (f"📦  {size_txt}", GREEN),
            (f"🗂  {mime}",       BLUE),
            (f"🔖  .{ext}" if ext else "🔖  ?", PURPLE),
        ]:
            tk.Label(meta_row, text=chip_txt, bg=CARD2, fg=chip_col,
                     font=_f(9,"bold"), padx=8, pady=3,
                     relief="flat").pack(side="left", padx=(0,6))

        # duration chip (if available)
        if meta.get("duration"):
            m, s = divmod(int(meta["duration"]), 60)
            h, m = divmod(m, 60)
            dur_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
            tk.Label(meta_row, text=f"⏱  {dur_str}", bg=CARD2, fg=YELLOW,
                     font=_f(9,"bold"), padx=8, pady=3).pack(side="left", padx=(0,6))

        # ── thumbnail preview for video/audio yt-dlp results ───────────────
        if meta.get("thumbnail"):
            self._load_thumbnail(meta["thumbnail"], frame)

        # ── image preview for direct image files ───────────────────────────
        elif ext in ("jpg","jpeg","png","gif","webp","bmp"):
            self._load_image_preview(meta["url"], frame)

        # ── description ────────────────────────────────────────────────────
        if meta.get("description"):
            tk.Frame(frame, bg=BORDER, height=1).pack(fill="x", pady=(10,6))
            desc = meta["description"]
            if len(desc) > 200:
                desc = desc[:200] + "…"
            tk.Label(frame, text=desc, bg=CARD, fg=MUTED,
                     font=_f(9,"bold"), wraplength=640, justify="left",
                     anchor="w").pack(anchor="w")

        # ── filename row ───────────────────────────────────────────────────
        tk.Frame(frame, bg=BORDER, height=1).pack(fill="x", pady=(10,6))
        fn_row = tk.Frame(frame, bg=CARD)
        fn_row.pack(fill="x", pady=(0,6))
        tk.Label(fn_row, text="Save as:", bg=CARD, fg=MUTED,
                 font=_f(9,"bold"), width=9, anchor="w").pack(side="left")
        self._dl_filename_var = tk.StringVar(value=meta.get("filename","download"))
        tk.Entry(fn_row, textvariable=self._dl_filename_var,
                 bg="#0d1320", fg=FG, insertbackground=FG,
                 relief="flat", font=F_BODY,
                 highlightbackground=BORDER, highlightthickness=1,
                 highlightcolor=PURPLE).pack(side="left", fill="x", expand=True,
                                             ipady=8, padx=(0,10))

        # ── download button ────────────────────────────────────────────────
        dl_btn_row = tk.Frame(frame, bg=CARD)
        dl_btn_row.pack(fill="x", pady=(6,0))
        self._dl_go_btn = tk.Button(
            dl_btn_row,
            text=f"  ⬇  Download  {'(via yt-dlp)' if meta.get('ydlp_ok') else '(direct stream)'}",
            bg=PURPLE, fg=WHITE, activebackground="#7c5cbf", activeforeground=WHITE,
            relief="flat", font=_f(11,"bold"), cursor="hand2", padx=18,
            command=lambda m=meta: self._start_direct_download(m))
        self._dl_go_btn.pack(side="left", ipady=10)

        tk.Label(dl_btn_row,
                 text=f"→  {self._save_dir}",
                 bg=CARD, fg=MUTED, font=_f(9,"bold"),
                 wraplength=360, justify="left").pack(side="left", padx=12)

        self._log(f"✅  File info loaded — {title_txt}", "ok")

    def _load_thumbnail(self, thumb_url, parent):
        """Fetch a thumbnail image and display it in parent frame (background thread)."""
        try:
            from PIL import Image, ImageTk
            import urllib.request, io
        except ImportError:
            return  # PIL not installed — skip silently

        def _fetch():
            try:
                req = urllib.request.Request(
                    thumb_url,
                    headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as r:
                    data = r.read()
                img = Image.open(io.BytesIO(data))
                img.thumbnail((200, 112))
                photo = ImageTk.PhotoImage(img)
                self.after(0, lambda p=photo: self._place_thumb(p, parent))
            except Exception:
                pass

        threading.Thread(target=_fetch, daemon=True).start()

    def _load_image_preview(self, url, parent):
        """Fetch and display a direct image file as a preview."""
        try:
            from PIL import Image, ImageTk
            import urllib.request, io
        except ImportError:
            return

        def _fetch():
            try:
                req = urllib.request.Request(
                    url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=15) as r:
                    data = r.read(1024 * 512)  # read up to 512 KB for preview
                img = Image.open(io.BytesIO(data))
                orig_size = img.size
                img.thumbnail((320, 180))
                photo = ImageTk.PhotoImage(img)
                self.after(0, lambda p=photo, s=orig_size: self._place_thumb(p, parent, s))
            except Exception:
                pass

        threading.Thread(target=_fetch, daemon=True).start()

    def _place_thumb(self, photo, parent, orig_size=None):
        """Place a PhotoImage thumbnail into parent (must run on main thread)."""
        thumb_frame = tk.Frame(parent, bg=CARD)
        thumb_frame.pack(anchor="w", pady=(10, 0))
        lbl = tk.Label(thumb_frame, image=photo, bg=CARD)
        lbl.image = photo   # keep reference
        lbl.pack(side="left")
        if orig_size:
            tk.Label(thumb_frame,
                     text=f"   {orig_size[0]}×{orig_size[1]} px",
                     bg=CARD, fg=MUTED, font=_f(9,"bold")).pack(side="left", padx=8)

    def _start_direct_download(self, meta):
        """Queue the actual download from the preview panel."""
        if self._is_downloading:
            self._log("⚠  A download is already in progress.", "warn")
            return
        # Safety guard — never download if nothing downloadable was detected
        if not meta.get("is_direct") and not meta.get("ydlp_ok"):
            self._log("⚠  Cannot download — URL is not a file.", "warn")
            return
        # Refuse to stream HTML content as a file
        mime = meta.get("mime") or ""
        html_types = ("text/html", "text/xhtml", "text/xml",
                      "application/xhtml", "application/xml")
        if meta.get("is_direct") and not meta.get("ydlp_ok") and \
                any(t in mime for t in html_types):
            self._log("⚠  Refusing to download HTML content as a file.", "warn")
            return
        filename = self._dl_filename_var.get().strip() or (meta.get("filename") or "download")
        url = meta["url"]
        if meta.get("ydlp_ok") and not meta.get("is_direct"):
            # Delegate to yt-dlp engine with chosen quality
            dest = os.path.join(self._save_dir, filename)
            import re
            safe = re.sub(r'[<>:"/\\|?*]', '_', os.path.splitext(filename)[0])
            dest_folder = self._save_dir
            self._is_downloading  = True
            self._completed = 0; self._errors = 0
            self._paused = False; self._pause_evt.set()
            self.after(0, lambda: self._lbl_done.config(text="0"))
            self.after(0, lambda: self._lbl_err.config(text="0"))
            self._set_btn(False)
            self.after(0, self._reset)
            self.after(0, lambda: self.status_var.set("Downloading…"))
            threading.Thread(target=self._download,
                             args=(url, None, False), daemon=True).start()
        else:
            # Direct HTTP stream
            save_path = os.path.join(self._save_dir, filename)
            self._is_downloading  = True
            self._completed = 0; self._errors = 0
            self._paused = False; self._pause_evt.set()
            self.after(0, lambda: self._lbl_done.config(text="0"))
            self.after(0, lambda: self._lbl_err.config(text="0"))
            self._set_btn(False)
            self.after(0, self._reset)
            self.after(0, lambda: self.status_var.set("Downloading…"))
            self._log(f"⬇  Downloading: {filename}", "head")
            threading.Thread(target=self._do_direct_download,
                             args=(url, save_path, meta.get("size")),
                             daemon=True).start()

    def _do_direct_download(self, url, save_path, total_size):
        """Stream a direct file URL to disk with live progress."""
        import urllib.request
        cloud_mirror = self._cloud_mirror
        cloud_label  = self._cloud_label
        save_dir_snap = self._save_dir
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            with urllib.request.urlopen(req, timeout=30) as resp, \
                 open(save_path, "wb") as fout:
                # try to get total from response headers if not already known
                if not total_size:
                    try: total_size = int(resp.headers.get("Content-Length", 0))
                    except: total_size = 0

                downloaded = 0
                chunk_size = 1024 * 256   # 256 KB chunks
                import time
                t0 = time.time()
                while True:
                    self._pause_evt.wait()
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    fout.write(chunk)
                    downloaded += len(chunk)
                    elapsed = time.time() - t0 or 0.001
                    speed   = downloaded / elapsed          # bytes/sec
                    if total_size:
                        pct    = downloaded / total_size * 100
                        remain = (total_size - downloaded) / speed if speed else 0
                        eta    = f"{int(remain // 60)}:{int(remain % 60):02d}"
                        pct_s  = f"{pct:.1f}%"
                    else:
                        pct     = 0
                        eta     = ""
                        pct_s   = self._fmt_size(downloaded)
                    spd_s = (f"{self._fmt_size(speed)}/s")
                    self._set_progress(pct, pct_s, spd_s, eta)
                    fn = os.path.basename(save_path)
                    self.after(0, lambda f=fn: self.file_label.config(text=f, fg=FG))

            actual   = os.path.getsize(save_path)
            sz_str   = self._fmt_size(actual)
            fn       = os.path.basename(save_path)
            self._log(f"✔  {fn}  ({sz_str})", "ok")
            self._completed += 1
            self.after(0, lambda v=self._completed: self._lbl_done.config(text=str(v)))
            self._set_progress(100, "100%", "", "")
            self.after(0, lambda: self.file_label.config(text="✔  Download complete!", fg=GREEN))
            self.after(0, lambda: self.status_var.set(f"Saved → {save_path}"))
            # cloud mirror
            if cloud_mirror and os.path.isfile(save_path):
                def _do_copy(fp=save_path):
                    try:
                        import shutil
                        rel        = os.path.relpath(fp, save_dir_snap)
                        cloud_dest = os.path.join(cloud_mirror, rel)
                        os.makedirs(os.path.dirname(cloud_dest), exist_ok=True)
                        shutil.copy2(fp, cloud_dest)
                        self._log(f"☁  Copied to {cloud_label}: {os.path.basename(cloud_dest)}", "info")
                    except Exception as ce:
                        self._log(f"⚠  Cloud copy failed: {ce}", "warn")
                threading.Thread(target=_do_copy, daemon=True).start()
        except Exception as ex:
            self._log(f"✘  Download failed: {ex}", "err")
            self._errors += 1
            self.after(0, lambda v=self._errors: self._lbl_err.config(text=str(v)))
            self.after(0, lambda: self.status_var.set("Error — see log."))
        finally:
            self._is_downloading = False
            self._set_btn(True)
            self.after(0, lambda: self._lbl_speed_sb.config(text="—"))
            self.after(0, lambda: self._lbl_eta_sb.config(text="—"))

    # ── YouTube Playlist download ────────────────────────────────────────────
    def _start(self):
        if self._is_downloading:
            self._log("⚠  A download is already in progress.", "warn")
            return
        url = self.url_var.get().strip()
        if not url:
            self._log("⚠  Please paste a YouTube playlist URL first.", "warn")
            return
        self._log("🔍  Fetching playlist info…", "info")
        self.btn.config(text="  ⏳  Fetching…", bg="#5a0e14", state="disabled")
        threading.Thread(target=self._prefetch_and_confirm,
                         args=(url,), daemon=True).start()

    def _prefetch_and_confirm(self, url):
        try:
            opts = {
                "quiet": True, "no_warnings": True,
                "extract_flat": True, "skip_download": True,
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            title   = info.get("title") or "Playlist"
            entries = info.get("entries") or []
            count   = len(entries)
            self.after(0, lambda: self._confirm_and_start(url, title, count))
        except Exception as ex:
            self.after(0, lambda e=ex: self._log(f"✘  Could not fetch playlist: {e}", "err"))
            self.after(0, lambda: self.btn.config(
                text="  ⬇  Start Download", bg=RED, state="normal"))

    def _confirm_and_start(self, url, title, count):
        # Restore button state while dialog is open
        self.btn.config(text="  ⬇  Start Download", bg=RED, state="normal")

        diag = tk.Toplevel(self)
        diag.title("Confirm Playlist Download")
        diag.configure(bg=CARD)
        diag.resizable(False, False)
        diag.grab_set()
        diag.transient(self)

        # centre on parent
        self.update_idletasks()
        pw, ph = self.winfo_width(), self.winfo_height()
        px, py = self.winfo_rootx(), self.winfo_rooty()
        dw, dh = 520, 260
        diag.geometry(f"{dw}x{dh}+{px + (pw-dw)//2}+{py + (ph-dh)//2}")

        pad = dict(padx=22, pady=6)
        tk.Label(diag, text="📋  Ready to Download", bg=CARD, fg=WHITE,
                 font=_f(13,"bold")).pack(anchor="w", **pad)
        tk.Label(diag, text=f"Playlist:  {title}", bg=CARD, fg=FG,
                 font=_f(10)).pack(anchor="w", padx=22, pady=(0,2))
        tk.Label(diag, text=f"Videos:    {count}", bg=CARD, fg=MUTED,
                 font=_f(10)).pack(anchor="w", padx=22, pady=(0,8))

        sep = tk.Frame(diag, bg=BORDER, height=1)
        sep.pack(fill="x", padx=22, pady=(0,10))

        tk.Label(diag, text="Save to:", bg=CARD, fg=MUTED,
                 font=F_LABEL).pack(anchor="w", padx=22)
        folder_var = tk.StringVar(value=self._save_dir)
        frow = tk.Frame(diag, bg=CARD)
        frow.pack(fill="x", padx=22, pady=(4,16))
        tk.Entry(frow, textvariable=folder_var, bg="#0d1320", fg=FG,
                 insertbackground=FG, relief="flat", font=F_BODY,
                 highlightbackground=BORDER, highlightthickness=1,
                 highlightcolor=RED).pack(side="left", fill="x", expand=True,
                                          ipady=8, padx=(0,8))
        tk.Button(frow, text="Browse…", bg=CARD2, fg=FG, relief="flat",
                  font=_f(9,"bold"), cursor="hand2", padx=8,
                  command=lambda: folder_var.set(
                      filedialog.askdirectory(initialdir=folder_var.get()) or folder_var.get())
                  ).pack(side="right", ipady=8)

        btn_row = tk.Frame(diag, bg=CARD)
        btn_row.pack(fill="x", padx=22, pady=(0,16))

        def _go():
            dest = folder_var.get().strip() or self._save_dir
            os.makedirs(dest, exist_ok=True)
            self._save_dir = dest
            diag.destroy()
            self._is_downloading = True
            self._set_btn(False)
            threading.Thread(target=self._download,
                             args=(url, dest), daemon=True).start()

        tk.Button(btn_row, text="  ✘  Cancel",
                  bg=CARD2, fg=MUTED, relief="flat",
                  font=_f(10,"bold"), cursor="hand2", padx=14,
                  command=diag.destroy).pack(side="left")
        tk.Button(btn_row, text="  ⬇  Start Download",
                  bg=RED, fg=WHITE, activebackground=RED2,
                  activeforeground=WHITE, relief="flat",
                  font=_f(10,"bold"), cursor="hand2", padx=14,
                  command=_go).pack(side="right")

    def _download(self, url, dest_folder=None, _batch=False):
        self._log(f"▶  {url}", "head")
        self.after(0, lambda: self.status_var.set("Downloading…"))
        save_dir_snapshot  = self._save_dir
        cloud_mirror       = self._cloud_mirror
        cloud_label        = self._cloud_label

        def hook(d):
            # Pause support — blocks between fragment downloads
            self._pause_evt.wait()
            if d["status"] == "downloading":
                ps = d.get("_percent_str","0%").strip()
                try: pct = float(ps.replace("%",""))
                except: pct = 0.0
                fn = os.path.basename(d.get("filename",""))
                self.after(0, lambda f=fn: self.file_label.config(text=f, fg=FG))
                self._set_progress(pct, ps,
                    d.get("_speed_str","").strip(),
                    d.get("_eta_str","").strip())
            elif d["status"] == "finished":
                fn       = os.path.basename(d.get("filename",""))
                filepath = d.get("filename","")
                # actual file size on disk
                try:
                    actual_sz = os.path.getsize(filepath) if filepath and os.path.isfile(filepath) else 0
                except Exception:
                    actual_sz = 0
                sz_str = f"  ({self._fmt_size(actual_sz)})" if actual_sz else ""
                self._log(f"✔  {fn}{sz_str}", "ok")
                self._completed += 1
                self.after(0, lambda v=self._completed: self._lbl_done.config(text=str(v)))
                self._set_progress(0,"0%","","")
                # Auto-copy to cloud mirror if folder was outside cloud root
                if cloud_mirror and filepath and os.path.isfile(filepath):
                    def _do_copy(fp=filepath):
                        try:
                            import shutil
                            rel        = os.path.relpath(fp, save_dir_snapshot)
                            cloud_dest = os.path.join(cloud_mirror, rel)
                            os.makedirs(os.path.dirname(cloud_dest), exist_ok=True)
                            shutil.copy2(fp, cloud_dest)
                            self._log(f"☁  Copied to {cloud_label}: {os.path.basename(cloud_dest)}", "info")
                        except Exception as ce:
                            self._log(f"⚠  Cloud copy failed: {ce}", "warn")
                    threading.Thread(target=_do_copy, daemon=True).start()
            elif d["status"] == "error":
                self._log(f"✘  {d.get('filename','')}", "err")
                self._errors += 1
                self.after(0, lambda v=self._errors: self._lbl_err.config(text=str(v)))

        class UL:
            def __init__(s, a): s.a=a
            def debug(s,m):
                if "[debug]" in m: return
                s.a._log(m,"norm")
            def info(s,m):    s.a._log(m,"info")
            def warning(s,m): s.a._log(f"[WARNING] {m}","warn")
            def error(s,m):   s.a._log(f"[ERROR] {m}","err")

        QUALITY_FMT = {
            "Best Quality":       "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
            "1080p":              "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=1080]+bestaudio/best",
            "720p":               "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=720]+bestaudio/best",
            "480p":               "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=480]+bestaudio/best",
            "360p":               "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=360]+bestaudio/best",
            "Audio Only (MP3)":   "bestaudio/best",
        }
        chosen_q = self.quality_var.get()
        fmt = QUALITY_FMT.get(chosen_q, QUALITY_FMT["Best Quality"])
        self._log(f"▶  Quality: {chosen_q}", "info")

        # Use pre-created dest_folder if provided, else fall back to save_dir/%(playlist_title)s
        if dest_folder:
            out_tpl = os.path.join(dest_folder, "%(playlist_index)s - %(title)s.%(ext)s")
        else:
            out_tpl = os.path.join(self._save_dir, "%(playlist_title)s", "%(playlist_index)s - %(title)s.%(ext)s")

        opts = {
            "outtmpl": out_tpl,
            "format": fmt,
            "merge_output_format": "mp3" if chosen_q == "Audio Only (MP3)" else "mp4",
            "concurrent_fragment_downloads": 8,
            "buffersize": 1024*256,
            "http_chunk_size": 1024*1024*10,
            "retries": 5,
            "fragment_retries": 5,
            "logger": UL(self),
            "progress_hooks": [hook],
            "ignoreerrors": True,
            "noplaylist": False,
        }
        if chosen_q == "Audio Only (MP3)":
            opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
            opts["outtmpl"] = out_tpl
        if FFMPEG_LOC:
            opts["ffmpeg_location"] = FFMPEG_LOC

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            self._log(f"🎉  Done! {self._completed} downloaded, {self._errors} errors.", "ok")
            self.after(0, lambda: self.file_label.config(text="✔  All complete!", fg=GREEN))
            self._set_progress(100,"100%","","")
            self.after(0, lambda d=self._save_dir: self.status_var.set(f"Saved → {d}"))
        except Exception as ex:
            self._log(f"✘  {ex}", "err")
            self.after(0, lambda: self.status_var.set("Error — see log."))
        finally:
            if not _batch:
                self._is_downloading = False
                self._set_btn(True)
                self.after(0, lambda: self._lbl_speed_sb.config(text="—"))
                self.after(0, lambda: self._lbl_eta_sb.config(text="—"))


if __name__ == "__main__":
    App().mainloop()

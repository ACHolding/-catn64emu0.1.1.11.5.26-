#!/usr/bin/env python3
"""CatN64Emu 0.1.1.a — N64 ROM launcher inspired by Parallel Launcher.
© AC Kondo 1999–2026. RetroArch/libretro backend with portable runtime support.

0.1.1.a
  * Sidebar/toolbar buttons keep their own styling through theme passes.
  * Emulator and core paths expand "~" consistently.
  * Library scan runs off the main thread and validates ZIP contents.
  * Zip extractions get a per-launch directory, removed when the game exits.
  * Deterministic temp names (sha1 of path + mtime).
  * Encrypted / corrupt archives report a real message.
  * Non-macOS install guidance instead of a Homebrew-only hard stop.
"""
from __future__ import annotations
import hashlib
import html.parser
import io
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import urllib.parse
import urllib.request
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from zipfile import ZipFile, BadZipFile

APP = 'CatN64Emu 0.1.1.a'
CONFIG_DIR = Path.home() / '.catn64emu'
CONFIG_FILE = CONFIG_DIR / 'settings.json'
EXTS = {'.z64', '.n64', '.v64'}
ZIP_EXTS = EXTS | {'.zip'}
DEFAULTS = {'rom_dirs': [], 'emulator': '', 'core': '', 'extra_args': '',
            'geometry': '1120x740', 'controller_profile': 'Auto (RetroArch)',
            'fullscreen': False, 'theme': 'System'}
BUNDLE_DIR = Path(__file__).resolve().parent / 'catn64emu_runtime'
CORE_NAMES = ('mupen64plus_next_libretro', 'parallel_n64_libretro', 'mupen64plus_libretro')
CORE_EXTS = ('.dylib', '.so', '.dll')
TEMP_PREFIX = 'catn64emu_'
TEMP_MAX_AGE = 7 * 24 * 3600


# --------------------------------------------------------------------------- #
# settings / paths
# --------------------------------------------------------------------------- #
def expand(path) -> Path:
    """Expand ~ and return a Path, without touching the filesystem."""
    return Path(str(path)).expanduser()


def load_settings():
    try:
        obj = json.loads(CONFIG_FILE.read_text(encoding='utf-8'))
        return {**DEFAULTS, **obj} if isinstance(obj, dict) else DEFAULTS.copy()
    except (OSError, ValueError):
        return DEFAULTS.copy()


# --------------------------------------------------------------------------- #
# ROM header parsing
# --------------------------------------------------------------------------- #
def read_header(path):
    try:
        with path.open('rb') as f:
            data = f.read(0x40)
        if len(data) < 0x40:
            return None
        magic = data[:4]
        if magic in (b'\x80\x37\x12\x40', b'\x80\x37\x12\x41'):
            raw = data
        elif magic == b'\x37\x80\x40\x12':
            raw = bytes(v for i in range(0, len(data) - 1, 2) for v in (data[i + 1], data[i]))
        elif magic == b'\x40\x12\x37\x80':
            raw = bytes(v for i in range(0, len(data) - 3, 4) for v in data[i:i + 4][::-1])
        else:
            return None
        name = raw[0x20:0x34].decode('ascii', 'replace').strip('\x00 ').strip()
        return name or path.stem
    except OSError:
        return None


def zip_has_rom(path):
    """Cheap pre-check so archives of documents never reach the library."""
    try:
        with ZipFile(path) as archive:
            return any(not i.is_dir() and Path(i.filename).suffix.lower() in EXTS
                       for i in archive.infolist())
    except (BadZipFile, OSError, RuntimeError):
        return False


# --------------------------------------------------------------------------- #
# backend discovery
# --------------------------------------------------------------------------- #
def discover_emulator():
    """Prefer a private RetroArch runtime, then a system RetroArch install."""
    candidates = [BUNDLE_DIR / 'retroarch', BUNDLE_DIR / 'retroarch.exe',
                  BUNDLE_DIR / 'RetroArch.app' / 'Contents' / 'MacOS' / 'RetroArch',
                  Path('/Applications/RetroArch.app/Contents/MacOS/RetroArch'),
                  Path.home() / 'Applications/RetroArch.app/Contents/MacOS/RetroArch']
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which('retroarch') or ''


def discover_core(exe=''):
    """Find a libretro N64 core without requiring manual -L arguments."""
    roots = [BUNDLE_DIR / 'cores', Path.home() / '.config/retroarch/cores',
             Path.home() / 'Library/Application Support/RetroArch/cores',
             Path('/Applications/RetroArch.app/Contents/Resources/cores'),
             Path('/Applications/RetroArch.app/Contents/MacOS/cores'),
             Path.home() / 'Library/Application Support/RetroArch/RetroArch.app/Contents/Resources/cores',
             Path('/usr/lib/libretro'), Path('/usr/local/lib/libretro'),
             Path('/opt/homebrew/lib/libretro')]
    if exe:
        e = expand(exe)
        roots += [e.parent / 'cores', e.parent.parent / 'Resources' / 'cores',
                  e.parent.parent.parent / 'Resources' / 'cores']
    for root in roots:
        for name in CORE_NAMES:
            for ext in CORE_EXTS:
                path = root / (name + ext)
                if path.is_file():
                    return str(path)
    return ''


def discover_standalone():
    """Native N64 fallback; does not need a libretro core."""
    paths = [BUNDLE_DIR / 'mupen64plus', Path('/opt/homebrew/bin/mupen64plus'),
             Path('/usr/local/bin/mupen64plus'), Path('/usr/bin/mupen64plus')]
    found = shutil.which('mupen64plus')
    if found:
        paths.insert(0, Path(found))
    return str(next((x for x in paths if x.is_file()), ''))


def resolve_core(configured, exe):
    """Honour an explicitly configured core, else search."""
    candidate = expand(configured) if configured else None
    if candidate and candidate.is_file():
        return str(candidate)
    return discover_core(exe)


def valid_retroarch(exe):
    """Accept anything runnable; the name check is a soft preference only."""
    if not exe:
        return False
    path = expand(exe)
    if path.is_file():
        return os.access(path, os.X_OK) or os.name == 'nt'
    return bool(shutil.which(exe))


def retroarch_core_available(exe, core):
    return valid_retroarch(exe) and bool(core) and expand(core).is_file()


# --------------------------------------------------------------------------- #
# core download
# --------------------------------------------------------------------------- #
class _Links(html.parser.HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            url = dict(attrs).get('href', '')
            if url:
                self.links.append(url)


def _official_core_indexes():
    """RetroArch's official buildbot; exact architecture matches only."""
    osname = platform.system()
    arch = platform.machine().lower()
    if osname == 'Darwin':
        return ['https://buildbot.libretro.com/nightly/apple/osx/arm64/latest/'] \
            if arch in ('arm64', 'aarch64') \
            else ['https://buildbot.libretro.com/nightly/apple/osx/x86_64/latest/']
    if osname == 'Windows':
        return ['https://buildbot.libretro.com/nightly/windows/x86_64/latest/'] \
            if arch in ('amd64', 'x86_64') else []
    if osname == 'Linux' and arch in ('x86_64', 'amd64'):
        return ['https://buildbot.libretro.com/nightly/linux/x86_64/latest/']
    return []


def _link_name(href):
    """Basename of an href, unquoted, without query string."""
    return urllib.parse.unquote(Path(href.split('?')[0]).name)


def download_n64_core(progress=lambda msg: None):
    """Install an official, architecture-matched libretro core."""
    indexes = _official_core_indexes()
    if not indexes:
        raise RuntimeError('No official core download index configured for this OS/CPU architecture.')
    last = 'No compatible N64 core archive was listed.'
    limit = 90 * 1024 * 1024
    for base in indexes:
        progress('Checking official RetroArch buildbot...')
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(base, headers={'User-Agent': 'CatN64Emu/0.1'}), timeout=20) as r:
                listing = r.read(2_000_000).decode('utf-8', 'replace')
            parser = _Links()
            parser.feed(listing)
            accepted = {n + s for n in CORE_NAMES for s in ('.zip', '.dylib.zip', '.so.zip',
                                                            '.dll.zip', '.dylib', '.so', '.dll')}
            for name in CORE_NAMES:
                matches = [u for u in parser.links if _link_name(u).lower() in accepted]
                if not matches:
                    continue
                href = matches[0]
                label = _link_name(href)
                url = urllib.parse.urljoin(base, href)
                progress('Downloading ' + label + '...')
                with urllib.request.urlopen(
                        urllib.request.Request(url, headers={'User-Agent': 'CatN64Emu/0.1'}), timeout=90) as r:
                    payload = r.read(limit + 1)
                if len(payload) > limit:
                    raise RuntimeError('Core download exceeds safety limit')
                if Path(label).suffix.lower() in CORE_EXTS:
                    dest = BUNDLE_DIR / 'cores' / label
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dest.with_suffix(dest.suffix + '.partial')
                    tmp.write_bytes(payload)
                    tmp.replace(dest)
                    progress('N64 core installed: ' + dest.name)
                    return str(dest)
                with ZipFile(io.BytesIO(payload)) as z:
                    members = [m for m in z.infolist()
                               if Path(m.filename).name.startswith(name)
                               and Path(m.filename).suffix.lower() in CORE_EXTS
                               and m.file_size < limit]
                    if not members:
                        raise RuntimeError('Archive contains no recognized N64 libretro core')
                    member = members[0]
                    dest = BUNDLE_DIR / 'cores' / Path(member.filename).name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    temp = dest.with_suffix(dest.suffix + '.partial')
                    with z.open(member) as src, temp.open('wb') as dst:
                        shutil.copyfileobj(src, dst)
                    temp.replace(dest)
                progress('N64 core installed: ' + dest.name)
                return str(dest)
        except Exception as e:
            last = str(e)
    raise RuntimeError('Could not install an official compatible N64 core. ' + last +
                       '\nSome platforms, including Apple Silicon, may not have a core available '
                       'on the official buildbot.')


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
class Launcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.cfg = load_settings()
        self.title(APP + '  |  AC Kondo Solutions')
        self.geometry(self.cfg.get('geometry') or DEFAULTS['geometry'])
        self.minsize(760, 480)
        self.emulator = tk.StringVar(value=self.cfg.get('emulator') or discover_emulator())
        self.extra_args = tk.StringVar(value=self.cfg.get('extra_args', ''))
        # Core discovery touches a dozen directories; defer it past first paint.
        self.core = tk.StringVar(value=self.cfg.get('core', ''))
        self.controller_profile = tk.StringVar(value=self.cfg.get('controller_profile', 'Auto (RetroArch)'))
        self.fullscreen = tk.BooleanVar(value=self.cfg.get('fullscreen', False))
        self.theme_choice = tk.StringVar(value=self.cfg.get('theme', 'System'))
        self.search = tk.StringVar()
        self.search.trace_add('write', lambda *_: self.populate())
        self.games = []
        self.visible = {}
        self._btn_role = {}
        self._scanning = False
        self.tempdir = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
        self._make_ui()
        self.apply_theme()
        self.scan()
        self.after(60, self._deferred_core_detect)
        self.protocol('WM_DELETE_WINDOW', self.close)

    def _deferred_core_detect(self):
        if not self.core.get().strip():
            found = discover_core(self.emulator.get())
            if found:
                self.core.set(found)

    # ---------------------------------------------------------------- layout
    def _make_ui(self):
        """Parallel Launcher-inspired three-pane library with launch and controller tools."""
        panel, navy, accent = '#ffffff', '#142e52', '#2b70c9'
        style = ttk.Style(self)
        if 'clam' in style.theme_names():
            style.theme_use('clam')
        style.configure('Treeview', font=('Arial', 11), rowheight=32,
                        background='white', fieldbackground='white')
        style.configure('Treeview.Heading', font=('Arial', 10, 'bold'))
        style.map('Treeview', background=[('selected', '#d8e8ff')],
                  foreground=[('selected', '#102a50')])

        menu = tk.Menu(self)
        filemenu = tk.Menu(menu, tearoff=False)
        for label, fn in [('Add ROM Folder…', self.add_folder), ('Open ROM…', self.open_file),
                          ('Refresh Library', self.scan)]:
            filemenu.add_command(label=label, command=fn)
        filemenu.add_separator()
        filemenu.add_command(label='Exit', command=self.close)
        menu.add_cascade(label='File', menu=filemenu)
        self.file_menu = filemenu

        tools = tk.Menu(menu, tearoff=False)
        tools.add_command(label='RetroArch / Emulator Settings…', command=self.configure_emulator)
        tools.add_command(label='Auto-detect Backend', command=self.autodetect)
        tools.add_command(label='Download N64 Core', command=self.install_core)
        tools.add_command(label='Install Native Backend', command=self.install_native_backend)
        tools.add_command(label='Controller Setup…', command=self.controller_setup)
        menu.add_cascade(label='Tools', menu=tools)
        self.tools_menu = tools

        appearance = tk.Menu(menu, tearoff=False)
        for choice in ('Light', 'Dark', 'System'):
            appearance.add_radiobutton(label=choice, variable=self.theme_choice,
                                       value=choice, command=self.change_theme)
        menu.add_cascade(label='Appearance', menu=appearance)
        self.appearance_menu = appearance
        self.menu_bar = menu

        helpmenu = tk.Menu(menu, tearoff=False)
        helpmenu.add_command(label='About', command=lambda: messagebox.showinfo(APP,
            APP + ' — Python N64 launcher\n© AC Kondo 1999–2026\n\n'
            'Parallel Launcher-inspired layout. Independent project, not an official port.\n'
            'Uses RetroArch/libretro or native Mupen64Plus.'))
        menu.add_cascade(label='Help', menu=helpmenu)
        self.help_menu = helpmenu
        self.config(menu=menu)

        header = tk.Frame(self, bg=navy, padx=22, pady=16)
        header.pack(fill='x')
        self.header_frame = header
        tk.Label(header, text='CATN64EMU 0.1.1.a', font=('Arial', 25, 'bold'),
                 fg='white', bg=navy).pack(side='left')
        tk.Label(header, text='N64 GAME LIBRARY  •  AC KONDO SOLUTIONS',
                 font=('Arial', 10, 'bold'), fg='#b8d9ff', bg=navy).pack(side='right')

        toolbar = tk.Frame(self, bg='#dfe9f5', padx=14, pady=10)
        toolbar.pack(fill='x')
        self.toolbar_frame = toolbar
        for text, cmd in [('＋ Add Games', self.add_folder), ('Open ROM', self.open_file),
                          ('↻ Refresh', self.scan), ('🎮 Controllers', self.controller_setup)]:
            btn = tk.Button(toolbar, text=text, command=cmd, padx=9)
            btn.pack(side='left', padx=(0, 7))
            self._btn_role[btn] = 'toolbar'
        tk.Label(toolbar, text='Search', bg='#dfe9f5').pack(side='left', padx=(16, 6))
        ttk.Entry(toolbar, textvariable=self.search).pack(side='left', fill='x', expand=True)

        body = tk.PanedWindow(self, orient='horizontal', bg='#edf2f8', sashwidth=5, bd=0)
        body.pack(fill='both', expand=True, padx=12, pady=12)
        self.body_frame = body

        sidebar = tk.Frame(body, bg=panel, width=195,
                           highlightbackground='#ccd9e9', highlightthickness=1)
        body.add(sidebar, minsize=160)
        tk.Label(sidebar, text='LIBRARY', bg=panel, fg=navy,
                 font=('Arial', 12, 'bold'), padx=15, pady=16).pack(anchor='w')
        for title, cmd in [('All Games', self.scan), ('Add ROM Folder', self.add_folder),
                           ('Emulator Settings', self.configure_emulator),
                           ('Controller Setup', self.controller_setup)]:
            btn = tk.Button(sidebar, text=title, command=cmd, anchor='w', relief='flat',
                            bg=panel, fg=navy, padx=16, pady=10)
            btn.pack(fill='x')
            self._btn_role[btn] = 'sidebar'

        center = tk.Frame(body, bg=panel, highlightbackground='#ccd9e9', highlightthickness=1)
        body.add(center, minsize=380)
        tk.Label(center, text='YOUR GAMES', bg=panel, fg=navy,
                 font=('Arial', 12, 'bold'), padx=15, pady=13).pack(anchor='w')
        listbox = tk.Frame(center, bg=panel)
        listbox.pack(fill='both', expand=True, padx=10, pady=(0, 10))
        self.tree = ttk.Treeview(listbox, columns=('game', 'type', 'location'),
                                 show='headings', selectmode='browse')
        for key, name, width, stretch in [('game', 'Game', 260, True),
                                          ('type', 'Format', 75, False),
                                          ('location', 'Location', 260, True)]:
            self.tree.heading(key, text=name)
            self.tree.column(key, width=width, stretch=stretch)
        scroll = ttk.Scrollbar(listbox, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side='left', fill='both', expand=True)
        scroll.pack(side='right', fill='y')
        self.tree.bind('<Double-1>', lambda _: self.launch_selected())
        self.tree.bind('<Return>', lambda _: self.launch_selected())
        self.tree.bind('<<TreeviewSelect>>', lambda _: self.update_details())

        details = tk.Frame(body, bg=panel, width=255,
                           highlightbackground='#ccd9e9', highlightthickness=1)
        body.add(details, minsize=210)
        tk.Label(details, text='GAME DETAILS', bg=panel, fg=navy,
                 font=('Arial', 12, 'bold'), pady=16).pack()
        self.detail = tk.StringVar(value='Select a ROM to view details.')
        tk.Label(details, textvariable=self.detail, wraplength=225, justify='left',
                 bg=panel, fg='#3b526e', padx=12, pady=20).pack(fill='x')
        tk.Label(details, text='Controller Profile', bg=panel, fg=navy,
                 font=('Arial', 10, 'bold')).pack(pady=(10, 4))
        self.profiles = ttk.Combobox(details, textvariable=self.controller_profile,
                                     state='readonly', width=23,
                                     values=['Auto (RetroArch)', 'Atari / Classic', 'NES / SNES',
                                             'Nintendo Switch Pro', 'Xbox',
                                             'PlayStation 3 / 4 / 5', 'Generic USB / Bluetooth'])
        self.profiles.pack(padx=12, fill='x')
        self.profiles.bind('<<ComboboxSelected>>', lambda _: self.save())
        ttk.Checkbutton(details, text='Start fullscreen', variable=self.fullscreen,
                        command=self.save).pack(pady=10)
        self.play_button = tk.Button(details, text='▶  PLAY GAME', command=self.launch_selected,
                                     bg=accent, fg='white', font=('Arial', 12, 'bold'),
                                     padx=18, pady=12)
        self.play_button.pack(fill='x', padx=12, pady=(14, 8))
        self._btn_role[self.play_button] = 'primary'
        close_btn = tk.Button(details, text='Controller Setup', command=self.controller_setup)
        close_btn.pack(fill='x', padx=12)
        self._btn_role[close_btn] = 'sidebar'

        bottom = tk.Frame(self, bg='#dfe9f5', padx=16, pady=9)
        bottom.pack(fill='x')
        self.bottom_frame = bottom
        self.status = tk.StringVar(value='Ready')
        self.status_label = tk.Label(bottom, textvariable=self.status, bg='#dfe9f5',
                                     fg='#324b66', anchor='w')
        self.status_label.pack(fill='x')

    # ---------------------------------------------------------------- theme
    def system_is_dark(self):
        """Read the OS appearance when possible; use a light fallback."""
        try:
            if sys.platform == 'darwin':
                r = subprocess.run(['defaults', 'read', '-g', 'AppleInterfaceStyle'],
                                   capture_output=True, text=True, timeout=2)
                return r.returncode == 0 and 'Dark' in r.stdout
            if sys.platform.startswith('win'):
                import winreg
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                    r'Software\Microsoft\Windows\CurrentVersion\Themes\Personalize') as k:
                    return winreg.QueryValueEx(k, 'AppsUseLightTheme')[0] == 0
            return os.environ.get('GTK_THEME', '').lower().endswith(':dark') or \
                os.environ.get('COLOR_SCHEME', '').lower() == 'dark'
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return False

    def change_theme(self):
        self.apply_theme()
        self.save()

    def apply_theme(self):
        mode = self.theme_choice.get()
        dark = self.system_is_dark() if mode == 'System' else mode == 'Dark'
        c = ({'bg': '#151a24', 'panel': '#202838', 'header': '#101827',
              'toolbar': '#192333', 'fg': '#ecf2ff', 'muted': '#b1c1d8',
              'accent': '#397fe0', 'select': '#315887', 'border': '#384860',
              'input': '#111b2a'} if dark else
             {'bg': '#edf2f8', 'panel': '#ffffff', 'header': '#142e52',
              'toolbar': '#dfe9f5', 'fg': '#142e52', 'muted': '#3b526e',
              'accent': '#2b70c9', 'select': '#d8e8ff', 'border': '#ccd9e9',
              'input': '#ffffff'})
        self.configure(bg=c['bg'])
        st = ttk.Style(self)
        if 'clam' in st.theme_names():
            st.theme_use('clam')
        st.configure('Treeview', background=c['input'], fieldbackground=c['input'],
                     foreground=c['fg'], rowheight=32, bordercolor=c['border'])
        st.configure('Treeview.Heading', background=c['toolbar'], foreground=c['fg'],
                     font=('Arial', 10, 'bold'))
        st.map('Treeview', background=[('selected', c['select'])],
               foreground=[('selected', '#ffffff' if dark else c['header'])])
        st.configure('TFrame', background=c['panel'])
        st.configure('TLabel', background=c['panel'], foreground=c['fg'])
        st.configure('TButton', background='#141d2c', foreground='#66b5ff',
                     padding=(10, 7), font=('Arial', 10, 'bold'))
        st.map('TButton', background=[('pressed', '#263b58'), ('active', '#22334b'),
                                      ('disabled', '#202633')],
               foreground=[('pressed', '#b4deff'), ('active', '#91caff'),
                           ('disabled', '#71859d')])
        st.configure('TCheckbutton', background=c['panel'], foreground=c['fg'])
        st.configure('TEntry', fieldbackground=c['input'], foreground=c['fg'])
        st.configure('TCombobox', fieldbackground=c['input'], foreground=c['fg'])
        st.map('TCombobox', fieldbackground=[('readonly', c['input'])],
               foreground=[('readonly', c['fg'])])

        def repaint(widget):
            if isinstance(widget, tk.Menu):
                return
            if isinstance(widget, (tk.Toplevel, tk.Tk, tk.PanedWindow)):
                widget.configure(bg=c['bg'])
            elif isinstance(widget, tk.Frame):
                if widget is getattr(self, 'header_frame', None):
                    widget.configure(bg=c['header'])
                elif widget in (getattr(self, 'toolbar_frame', None),
                                getattr(self, 'bottom_frame', None)):
                    widget.configure(bg=c['toolbar'])
                elif widget is getattr(self, 'body_frame', None):
                    widget.configure(bg=c['bg'])
                else:
                    widget.configure(bg=c['panel'])
                if widget.cget('highlightthickness'):
                    widget.configure(highlightbackground=c['border'])
            elif isinstance(widget, tk.Label):
                master = widget.master
                parent_bg = master.cget('bg') if 'bg' in master.keys() else c['panel']
                if parent_bg == c['header']:
                    fg = '#ffffff'
                elif widget is getattr(self, 'status_label', None):
                    fg = c['muted']
                else:
                    fg = c['fg']
                widget.configure(bg=parent_bg, fg=fg)
            elif isinstance(widget, tk.Button):
                role = self._btn_role.get(widget)
                if role == 'primary':
                    widget.configure(bg=c['accent'], fg='#ffffff', relief='raised',
                                     activebackground=c['select'], activeforeground='#ffffff')
                elif role == 'toolbar':
                    widget.configure(bg=c['toolbar'], fg=c['fg'], relief='raised',
                                     activebackground=c['select'], activeforeground=c['fg'])
                else:
                    widget.configure(bg=c['panel'], fg=c['fg'], relief='flat',
                                     activebackground=c['select'], activeforeground=c['fg'])
                widget.configure(highlightbackground=c['border'], cursor='hand2')
            for child in widget.winfo_children():
                repaint(child)

        repaint(self)
        try:
            self.menu_bar.configure(bg=c['panel'], fg=c['fg'], activebackground=c['select'])
            for m in (self.file_menu, self.tools_menu, self.help_menu, self.appearance_menu):
                m.configure(bg=c['panel'], fg=c['fg'], activebackground=c['select'])
        except (AttributeError, tk.TclError):
            pass

    # ---------------------------------------------------------------- details
    def update_details(self):
        path = self.selected_path()
        if not path:
            self.detail.set('Select a ROM to view details.')
        else:
            self.detail.set(f'{path.stem}\n\nFormat: {path.suffix.upper().lstrip(".")}\n\n'
                            f'File: {path.name}\n\nDouble-click or press Play.')

    def controller_setup(self):
        dlg = tk.Toplevel(self)
        dlg.title('CatN64Emu — Controller Setup')
        dlg.geometry('590x440')
        dlg.transient(self)
        frame = ttk.Frame(dlg, padding=18)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Bluetooth & USB Controllers',
                  font=('Arial', 16, 'bold')).pack(anchor='w', pady=(0, 10))
        text = ('CatN64Emu uses RetroArch controller autoconfiguration when the RetroArch backend is active.\n\n'
                '1. Pair your controller in your OS Bluetooth settings.\n'
                '2. Connect via USB if Bluetooth is unsupported.\n'
                '3. Open RetroArch → Settings → Input → RetroPad Binds / Port 1 Controls.\n'
                '4. Use Online Updater → Update Controller Profiles if necessary.\n\n'
                'Supported when drivers and profiles exist: DualSense (PS5), DualShock, Xbox, Switch Pro, '
                'and many generic Bluetooth/USB gamepads.\n\n'
                'Vintage Atari controllers generally need a USB/Bluetooth adapter that exposes standard '
                'gamepad input. Not every Atari device is plug-and-play.\n\n'
                'Note: Native Mupen64Plus controller mapping is configured separately using its input plugin.')
        ttk.Label(frame, text=text, wraplength=535, justify='left').pack(anchor='w')
        ttk.Button(frame, text='Open Bluetooth Settings',
                   command=self.open_bluetooth).pack(anchor='w', pady=(15, 5))
        ttk.Button(frame, text='Close', command=dlg.destroy).pack(anchor='e')

    def open_bluetooth(self):
        try:
            if sys.platform == 'darwin':
                subprocess.Popen(['open', 'x-apple.systempreferences:com.apple.BluetoothSettings'])
            elif sys.platform.startswith('win'):
                subprocess.Popen(['start', 'ms-settings:bluetooth'], shell=True)
            else:
                messagebox.showinfo('Bluetooth', 'Open your system Bluetooth settings and pair your controller.')
        except OSError as exc:
            messagebox.showerror('Bluetooth settings', str(exc))

    # ---------------------------------------------------------------- settings
    def save(self):
        self.cfg['emulator'] = self.emulator.get().strip()
        self.cfg['core'] = self.core.get().strip()
        self.cfg['extra_args'] = self.extra_args.get().strip()
        self.cfg['controller_profile'] = self.controller_profile.get()
        self.cfg['fullscreen'] = self.fullscreen.get()
        self.cfg['geometry'] = self.geometry()
        self.cfg['theme'] = self.theme_choice.get()
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_FILE.write_text(json.dumps(self.cfg, indent=2), encoding='utf-8')
        except OSError as exc:
            self.status.set('Could not save settings: ' + str(exc))

    # ---------------------------------------------------------------- library
    def add_folder(self):
        folder = filedialog.askdirectory(title='Choose N64 ROM folder')
        if folder and folder not in self.cfg['rom_dirs']:
            self.cfg['rom_dirs'].append(folder)
            self.save()
            self.scan()

    def scan(self):
        """Walk configured folders on a worker thread; results land on the UI thread."""
        if self._scanning:
            return
        self._scanning = True
        self.status.set('Scanning library…')
        threading.Thread(target=self._scan_worker,
                         args=(list(self.cfg.get('rom_dirs', [])),), daemon=True).start()

    def _scan_worker(self, folders):
        self._sweep_temp_dirs()
        games, seen = [], set()
        for folder in folders:
            root = expand(folder)
            if not root.is_dir():
                continue
            try:
                for p in root.rglob('*'):
                    try:
                        if not p.is_file():
                            continue
                    except OSError:
                        continue
                    suffix = p.suffix.lower()
                    if suffix not in ZIP_EXTS:
                        continue
                    key = str(p)
                    if key in seen:
                        continue
                    seen.add(key)
                    if suffix == '.zip':
                        if not zip_has_rom(p):
                            continue
                        name = p.stem
                    else:
                        name = read_header(p)
                    if name:
                        games.append((name, p))
            except OSError:
                continue
        games.sort(key=lambda g: g[0].casefold())
        self.after(0, lambda: self._scan_done(games))

    def _scan_done(self, games):
        self.games = games
        self._scanning = False
        self.populate()

    def _sweep_temp_dirs(self):
        """Drop leftovers from crashed sessions; per-launch dirs clean up on exit."""
        try:
            now = time.time()
            for d in Path(tempfile.gettempdir()).glob(TEMP_PREFIX + '*'):
                if d.is_dir() and d != self.tempdir and now - d.stat().st_mtime > TEMP_MAX_AGE:
                    shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass

    def populate(self):
        if not hasattr(self, 'tree'):
            return
        self.tree.delete(*self.tree.get_children())
        self.visible.clear()
        query = self.search.get().strip().casefold()
        for name, path in self.games:
            if query and query not in (name + ' ' + path.name).casefold():
                continue
            item = self.tree.insert('', 'end', values=(name, path.suffix.upper().lstrip('.'), str(path)))
            self.visible[item] = path
        self.status.set(f'{len(self.visible)} games shown  •  {len(self.games)} total  •  '
                        'Double-click to launch')

    def open_file(self):
        f = filedialog.askopenfilename(title='Open Nintendo 64 ROM', filetypes=[
            ('N64 ROMs and ZIPs', '*.z64 *.n64 *.v64 *.zip'), ('All files', '*.*')])
        if f:
            self.launch(Path(f))

    def selected_path(self):
        selected = self.tree.selection()
        return self.visible.get(selected[0]) if selected else None

    def launch_selected(self):
        path = self.selected_path()
        if not path:
            messagebox.showinfo(APP, 'Select a game, or click Open ROM.')
            return
        self.launch(path)

    # ---------------------------------------------------------------- launching
    def prepare_rom(self, path):
        """Return (rom_path, workdir_or_None)."""
        if path.suffix.lower() != '.zip':
            if read_header(path) is None:
                raise ValueError('Unrecognized N64 ROM header.')
            return path, None
        try:
            with ZipFile(path) as archive:
                matches = [i for i in archive.infolist()
                           if not i.is_dir() and Path(i.filename).suffix.lower() in EXTS]
                if not matches:
                    raise ValueError('ZIP archive contains no .z64, .n64, or .v64 ROM.')
                item = matches[0]
                if item.flag_bits & 0x1:
                    raise ValueError('ZIP entry is encrypted; extract the ROM first.')
                if item.file_size > 256 * 1024 * 1024:
                    raise ValueError('ROM exceeds 256 MB safety limit.')
                try:
                    stamp = path.stat().st_mtime
                except OSError:
                    stamp = 0
                digest = hashlib.sha1(f'{path.resolve()}|{stamp}'.encode('utf-8')).hexdigest()[:12]
                work = self.tempdir / digest
                work.mkdir(parents=True, exist_ok=True)
                target = work / ('game' + Path(item.filename).suffix.lower())
                with archive.open(item) as src, target.open('wb') as dst:
                    shutil.copyfileobj(src, dst)
                if read_header(target) is None:
                    raise ValueError('Archive contains an unrecognized ROM.')
                return target, work
        except BadZipFile as exc:
            raise ValueError('Invalid ZIP archive.') from exc
        except RuntimeError as exc:
            raise ValueError(f'Could not read archive: {exc}') from exc

    def _watch_workdir(self, proc, work):
        """Remove an extraction directory only after the emulator exits."""
        def wait():
            try:
                proc.wait()
            except OSError:
                pass
            shutil.rmtree(work, ignore_errors=True)
        threading.Thread(target=wait, daemon=True).start()

    def launch(self, path):
        exe = self.emulator.get().strip() or discover_emulator()
        configured = self.core.get().strip()
        core = resolve_core(configured, exe)
        if core and core != configured:
            self.core.set(core)
            self.save()
        native = discover_standalone()
        if not retroarch_core_available(exe, core) and not native:
            if platform.system() == 'Darwin' and shutil.which('brew'):
                if messagebox.askyesno('Install native N64 backend?',
                        'No compatible RetroArch N64 core was found.\n\n'
                        'CatN64Emu can use native Mupen64Plus without a libretro core.\n'
                        'Install Mupen64Plus with Homebrew now?'):
                    self.install_native_backend(path)
                    return
            else:
                messagebox.showerror('N64 backend missing',
                        'No compatible N64 backend was found.\n\n'
                        'Install Mupen64Plus or install RetroArch and a matching N64 core.\n'
                        'CatN64Emu will automatically detect either backend.')
            return
        work = None
        try:
            rom, work = self.prepare_rom(path)
            if retroarch_core_available(exe, core):
                args = shlex.split(self.extra_args.get())
                if self.fullscreen.get() and '--fullscreen' not in args:
                    args.append('--fullscreen')
                target = expand(exe)
                proc = subprocess.Popen([str(target), '-L', str(expand(core)), *args, str(rom)],
                                        cwd=str(target.resolve().parent) if target.is_file() else None)
                self.status.set('RetroArch launched: ' + path.name)
            else:
                native_path = expand(native)
                proc = subprocess.Popen([str(native_path), str(rom)],
                                        cwd=str(native_path.parent))
                self.status.set('Native Mupen64Plus launched: ' + path.name)
        except (OSError, ValueError) as exc:
            if work:
                shutil.rmtree(work, ignore_errors=True)
            messagebox.showerror('Launch failed', str(exc))
            return
        if work:
            self._watch_workdir(proc, work)

    # ---------------------------------------------------------------- installers
    def install_native_backend(self, pending_rom=None):
        """Install a real emulator through the platform package manager."""
        osname = platform.system()
        if osname == 'Darwin':
            brew = shutil.which('brew')
            if not brew:
                messagebox.showerror('Homebrew required',
                    'Install Homebrew first, or install Mupen64Plus manually:\n\n'
                    'https://brew.sh')
                return
            command = [brew, 'install', 'mupen64plus']
        elif osname == 'Linux':
            hints = {'apt-get': ['sudo', 'apt-get', 'install', '-y', 'mupen64plus'],
                     'dnf': ['sudo', 'dnf', 'install', '-y', 'mupen64plus'],
                     'pacman': ['sudo', 'pacman', '-S', '--noconfirm', 'mupen64plus']}
            for tool, cmd in hints.items():
                if shutil.which(tool):
                    command = cmd
                    break
            else:
                messagebox.showerror('Package manager not found',
                    'Install Mupen64Plus with your distribution package manager, then retry.')
                return
        else:
            messagebox.showerror('Automatic install unavailable',
                'Download Mupen64Plus for your platform, or use RetroArch with an N64 core.')
            return
        self.status.set('Installing native Mupen64Plus…')
        def work():
            try:
                result = subprocess.run(command, capture_output=True, text=True, timeout=900)
                if result.returncode:
                    detail = (result.stderr or result.stdout)[-1600:]
                    raise RuntimeError(detail or 'Installation failed')
                exe = discover_standalone()
                if not exe:
                    raise RuntimeError('Package manager finished but mupen64plus was not found.')
                def success():
                    self.status.set('Mupen64Plus installed: ' + exe)
                    if pending_rom:
                        self.launch(pending_rom)
                    else:
                        messagebox.showinfo('N64 backend installed',
                                            'Mupen64Plus is ready. Select a ROM to play.')
                self.after(0, success)
            except Exception as exc:
                self.after(0, lambda msg=str(exc): messagebox.showerror(
                    'Mupen64Plus installation failed', msg))
        threading.Thread(target=work, daemon=True).start()

    def install_core(self):
        self.status.set('Checking for an official N64 core…')
        def progress(msg):
            self.after(0, lambda m=msg: self.status.set(m))
        def work():
            try:
                core = download_n64_core(progress)
                def done():
                    self.core.set(core)
                    self.save()
                    messagebox.showinfo('N64 core installed',
                        'Installed: ' + core + '\nYou can now launch games if RetroArch is installed.')
                self.after(0, done)
            except Exception as exc:
                self.after(0, lambda msg=str(exc): messagebox.showerror('Core installation failed', msg))
        threading.Thread(target=work, daemon=True).start()

    def autodetect(self):
        exe = discover_emulator()
        if exe:
            self.emulator.set(exe)
        core = discover_core(self.emulator.get())
        self.core.set(core)
        self.save()
        self.status.set(('RetroArch found' if exe else 'RetroArch not found') +
                        (' • N64 core found' if core
                         else (' • Native Mupen64Plus available' if discover_standalone()
                               else ' • N64 backend missing')))

    def configure_emulator(self):
        dialog = tk.Toplevel(self)
        dialog.title('RetroArch + N64 Core Setup')
        dialog.geometry('750x320')
        dialog.transient(self)
        dialog.grab_set()
        dialog.columnconfigure(1, weight=1)
        tk.Label(dialog, text='RetroArch executable:').grid(row=0, column=0, padx=12, pady=12, sticky='w')
        ttk.Entry(dialog, textvariable=self.emulator).grid(row=0, column=1, sticky='ew', padx=5)
        tk.Button(dialog, text='Browse…', command=self._browse_emulator).grid(row=0, column=2, padx=12)
        tk.Label(dialog, text='N64 libretro core:').grid(row=1, column=0, padx=12, pady=12, sticky='w')
        ttk.Entry(dialog, textvariable=self.core).grid(row=1, column=1, sticky='ew', padx=5)
        tk.Button(dialog, text='Browse…', command=self._browse_core).grid(row=1, column=2, padx=12)
        tk.Label(dialog, text='Extra RetroArch arguments:').grid(row=2, column=0, padx=12, pady=8, sticky='w')
        ttk.Entry(dialog, textvariable=self.extra_args).grid(row=2, column=1, columnspan=2, sticky='ew', padx=5)
        info = ('Portable runtime: put RetroArch in catn64emu_runtime/ and your N64 core in\n'
                'catn64emu_runtime/cores/. The launcher auto-selects the core with -L.\n'
                'Alternatively install RetroArch and download Mupen64Plus-Next in its Online Updater.\n'
                'If RetroArch has no compatible core, CatN64Emu uses native Mupen64Plus.\n'
                'Install N64 Backend uses your platform package manager.')
        tk.Label(dialog, text=info, justify='left').grid(row=3, column=0, columnspan=3,
                                                         padx=12, pady=12, sticky='w')
        tk.Button(dialog, text='Install N64 Backend',
                  command=self.install_native_backend).grid(row=4, column=0, padx=12, pady=8)
        tk.Button(dialog, text='Auto-detect',
                  command=self.autodetect).grid(row=4, column=1, sticky='e', pady=8)
        tk.Button(dialog, text='Save Settings',
                  command=lambda: (self.save(), dialog.destroy()),
                  padx=15).grid(row=4, column=2, padx=12, pady=8)

    def _browse_emulator(self):
        f = filedialog.askopenfilename(title='Choose RetroArch executable')
        if f:
            self.emulator.set(f)
            if not self.core.get():
                self.core.set(discover_core(f))

    def _browse_core(self):
        f = filedialog.askopenfilename(title='Choose N64 libretro core',
                                       filetypes=[('Libretro cores', '*.dylib *.so *.dll'),
                                                  ('All files', '*.*')])
        if f:
            self.core.set(f)

    def close(self):
        try:
            self.save()
        finally:
            # Per-launch extraction dirs are removed when the emulator exits;
            # this one goes on the next start if anything is still inside.
            try:
                shutil.rmtree(self.tempdir, ignore_errors=True)
            except OSError:
                pass
            self.destroy()


if __name__ == '__main__':
    try:
        Launcher().mainloop()
    except tk.TclError as exc:
        print('Tkinter GUI unavailable:', exc, file=sys.stderr)
        sys.exit(1)
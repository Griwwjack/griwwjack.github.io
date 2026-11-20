#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
#  PROJETO: µNotas
#  VERSÃO: 0.1.0
#  DATA: Novembro / 2026
#  DESCRIÇÃO: Bloco de notas para anotar coisas durante os jogos.
#             Pensado para o Steam Deck (1280x800, tela cheia, touch screen),
#             no mesmo estilo visual do µSave Backup.
#  TECNOLOGIAS: Python 3.9+, Tkinter (biblioteca padrão, sem dependências).
#
#  ORGANIZAÇÃO
#   • As notas ficam agrupadas por JOGO; “Todas as notas” mostra tudo, com busca.
#   • Cada nota tem título (opcional), texto, cor, fixação (pin) e datas.
#   • Dados em ~/.config/unotas/ (notas.json e config.json), sempre gravados de
#     forma atômica (arquivo temporário + rename), então nunca ficam pela metade.
#   • Exportar um jogo para Markdown (.md) e backup completo (.unotas.json), com
#     importação por mesclagem (serve para levar as notas a outro dispositivo).
# =============================================================================
import collections
import json
import math
import os
import re
import sys
import uuid
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import font as tkfont

APP_NAME = "µNotas"
APP_VER = "0.1.0"
CFG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "unotas"
CFG_FILE = CFG_DIR / "config.json"
NOTES_FILE = CFG_DIR / "notas.json"

BACKUP_KIND = "unotas-backup"
BACKUP_EXT = ".unotas.json"
MAX_RENDER = 200            # notas desenhadas de uma vez (use a busca para o resto)
NOTE_COLORS = [("none", None), ("red", "#e5484d"), ("orange", "#ff8a3d"), ("yellow", "#ffd800"),
               ("green", "#4cc38a"), ("blue", "#3b9eff"), ("magenta", "#d946ef")]
COLOR_MAP = dict(NOTE_COLORS)


# =============================================================================
#  MOTOR (sem dependência de interface)
# =============================================================================
def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def new_id():
    return uuid.uuid4().hex[:10]


def safe_name(name):
    s = re.sub(r"[^\w\-]+", "_", name, flags=re.UNICODE)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "jogo"


def parse_ts(s):
    try:
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return datetime.now()


def fmt_short(s):
    return parse_ts(s).strftime("%d/%m/%Y %H:%M")


def write_text_atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def write_json_atomic(path, data):
    write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False))


# ---------------------------------------------------------- configuração ----
def default_settings():
    return dict(theme="dark", fullscreen=True, scale=100, font_size=18,
                last_game=None, last_note_game=None, io_dir="")


def load_config():
    cfg = dict(settings=default_settings())
    try:
        data = json.loads(CFG_FILE.read_text(encoding="utf-8"))
        cfg["settings"].update(data.get("settings", {}))
    except (OSError, ValueError, AttributeError):
        pass
    return cfg


def save_config(cfg):
    try:
        write_json_atomic(CFG_FILE, cfg)
    except OSError as e:
        print("Falha ao salvar configuração:", e, file=sys.stderr)


# ----------------------------------------------------------------- dados ----
def norm_note(n, gids, fallback):
    """Normaliza uma nota vinda do disco/importação. Retorna None se inválida."""
    if not isinstance(n, dict):
        return None
    created = str(n.get("created") or now_iso())
    return dict(id=str(n.get("id") or new_id()),
                game=n.get("game") if n.get("game") in gids else fallback,
                title=str(n.get("title") or ""), text=str(n.get("text") or ""),
                created=created, modified=str(n.get("modified") or created),
                pinned=bool(n.get("pinned")),
                color=n.get("color") if n.get("color") in COLOR_MAP else "none")


def ensure_game(store):
    """Garante que exista ao menos um jogo; devolve o id do primeiro."""
    if not store["games"]:
        store["games"].append(dict(id=new_id(), name="Geral", created=now_iso()))
    return store["games"][0]["id"]


def load_store():
    games, notes, recovered = [], [], ""
    try:
        data = json.loads(NOTES_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("formato inesperado")
        for g in data.get("games") or []:
            if isinstance(g, dict) and g.get("id") and str(g.get("name") or "").strip():
                games.append(dict(id=str(g["id"]), name=str(g["name"]).strip(),
                                  created=str(g.get("created") or now_iso())))
        gids = {g["id"] for g in games}
        fallback = games[0]["id"] if games else None
        for n in data.get("notes") or []:
            n = norm_note(n, gids, fallback)
            if n and n["game"]:
                notes.append(n)
    except FileNotFoundError:
        pass
    except (OSError, ValueError, AttributeError):
        # Arquivo ilegível: guarda uma cópia em vez de sobrescrevê-lo em silêncio.
        bad = NOTES_FILE.with_name("notas.corrompido-%s.json" % datetime.now().strftime("%Y%m%d-%H%M%S"))
        try:
            os.replace(NOTES_FILE, bad)
            recovered = str(bad)
        except OSError:
            pass
        games, notes = [], []
    store = dict(games=games, notes=notes, recovered=recovered)
    ensure_game(store)
    return store


def save_store(store):
    """Grava as notas. Devolve None se deu certo ou a mensagem de erro."""
    try:
        write_json_atomic(NOTES_FILE, dict(version=1, games=store["games"], notes=store["notes"]))
        return None
    except OSError as e:
        print("Falha ao salvar notas:", e, file=sys.stderr)
        return str(e)


# ------------------------------------------------------- consulta / texto ----
def visible_notes(store, game_id, query=""):
    """Notas de um jogo (ou de todos, se game_id for None), filtradas pela busca.
    Fixadas primeiro; dentro de cada grupo, as editadas mais recentemente."""
    q = (query or "").casefold().strip()
    out = [n for n in store["notes"]
           if (game_id is None or n["game"] == game_id)
           and (not q or q in (n["title"] + "\n" + n["text"]).casefold())]
    out.sort(key=lambda n: n["modified"], reverse=True)
    out.sort(key=lambda n: not n["pinned"])
    return out


def _lines(text):
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def note_heading(n):
    if n["title"]:
        return n["title"]
    ls = _lines(n["text"])
    return (ls[0][:80] + ("…" if len(ls[0]) > 80 else "")) if ls else "(sem texto)"


def note_preview(n, limit=150):
    ls = _lines(n["text"])
    if not n["title"]:
        ls = ls[1:]
    s = " ".join(ls)
    return s[:limit].rstrip() + ("…" if len(s) > limit else "")


def game_markdown(game, notes):
    """Notas do jogo em Markdown, em ordem cronológica."""
    out = ["# %s" % game["name"], ""]
    for n in sorted(notes, key=lambda n: n["created"]):
        when = fmt_short(n["created"])
        out += ["## %s" % (n["title"] or "Nota — " + when), "*%s*%s" % (when, "  📌" if n["pinned"] else ""),
                "", n["text"].strip(), ""]
    return "\n".join(out).rstrip() + "\n"


# ----------------------------------------------------------------- backup ----
def backup_data(store):
    return dict(app=APP_NAME, kind=BACKUP_KIND, version=1, exported=now_iso(),
                games=store["games"], notes=store["notes"])


def read_backup_file(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as e:
        raise ValueError("Não foi possível ler o arquivo (%s)." % e)
    if not isinstance(data, dict) or data.get("kind") != BACKUP_KIND:
        raise ValueError("Este arquivo não é um backup exportado pelo µNotas.")
    return data


def merge_backup(store, data):
    """Mescla um backup nas notas atuais, sem apagar nada. Jogos são casados por id ou
    nome; notas por id (vence a editada mais recentemente). Devolve contagens."""
    by_id = {g["id"]: g for g in store["games"]}
    by_name = {g["name"].casefold(): g for g in store["games"]}
    gmap, res = {}, dict(games=0, new=0, upd=0)
    for g in data.get("games") or []:
        name = str(g.get("name") or "").strip() if isinstance(g, dict) else ""
        if not name or not g.get("id"):
            continue
        ex = by_id.get(str(g["id"])) or by_name.get(name.casefold())
        if ex is None:
            ex = dict(id=str(g["id"]), name=name, created=str(g.get("created") or now_iso()))
            store["games"].append(ex)
            by_id[ex["id"]] = ex
            by_name[name.casefold()] = ex
            res["games"] += 1
        gmap[str(g["id"])] = ex["id"]
    fallback = ensure_game(store)
    gids = {g["id"] for g in store["games"]}
    notes = {n["id"]: n for n in store["notes"]}
    for raw in data.get("notes") or []:
        if not isinstance(raw, dict):
            continue
        n = norm_note(dict(raw, game=gmap.get(str(raw.get("game")), raw.get("game"))), gids, fallback)
        ex = notes.get(n["id"])
        if ex is None:
            store["notes"].append(n)
            notes[n["id"]] = n
            res["new"] += 1
        elif n["modified"] > ex["modified"]:
            ex.update(n)
            res["upd"] += 1
    return res


# =============================================================================
#  TEMA + WIDGETS TOUCH (compartilhados com o µSave Backup)
# =============================================================================
THEMES = {
    "dark": dict(bg="#2e2e2e", card="#242424", ink="#eeeeec", mut="#a8a39d", line="#454545",
                 acc="#e95420", accink="#ffffff", sel="#5a2f20", head="#333333", bar="#2c001e",
                 barink="#ffffff", danger="#ff6b6b", dangerbg="#c7162b", overlay="#141414",
                 ok="#8ae234", hdrbtn="#4a2340", hdrpress="#6a4560"),
    "light": dict(bg="#f6f5f4", card="#ffffff", ink="#3d3d3d", mut="#77716b", line="#dcdad6",
                  acc="#e95420", accink="#ffffff", sel="#fad7c8", head="#ebe9e7", bar="#2c001e",
                  barink="#ffffff", danger="#c7162b", dangerbg="#c7162b", overlay="#6e6a66",
                  ok="#2e7d32", hdrbtn="#4a2340", hdrpress="#6a4560"),
}
C = dict(THEMES["dark"])
S = 1.0
FONT = dict(ui="sans-serif", mono="monospace")
TOUCH = None


def sz(n):
    return max(1, int(round(n * S)))


def F(px, bold=False, mono=False):
    return (FONT["mono" if mono else "ui"], -max(9, int(round(px * S))), "bold" if bold else "normal")


def shade(hexcolor, f):
    r, g, b = (int(hexcolor[i:i + 2], 16) for i in (1, 3, 5))
    if f < 1:
        r, g, b = int(r * f), int(g * f), int(b * f)
    else:
        r, g, b = (min(255, int(c + (255 - c) * (f - 1))) for c in (r, g, b))
    return "#%02x%02x%02x" % (r, g, b)


def fmt_dt(dt):
    return dt.strftime("%d/%m/%Y %H:%M:%S")


# =============================================================================
#  WIDGETS TOUCH
# =============================================================================
class Touch:
    """Rolagem por arraste (dedo) e roda do mouse; diferencia toque de arraste."""

    def __init__(self, root):
        self.root, self.moved, self.owner = root, False, None
        self.x0 = self.y0 = 0
        root.bind_all("<ButtonPress-1>", self.press, add="+")
        root.bind_all("<B1-Motion>", self.motion, add="+")
        root.bind_all("<Button-4>", lambda e: self.wheel(e, -1), add="+")
        root.bind_all("<Button-5>", lambda e: self.wheel(e, 1), add="+")
        root.bind_all("<MouseWheel>", lambda e: self.wheel(e, -1 if e.delta > 0 else 1), add="+")

    @staticmethod
    def find_owner(w):
        while w is not None:
            if getattr(w, "_is_scroll", False):
                return w
            w = getattr(w, "master", None)
        return None

    def press(self, e):
        self.moved, self.owner = False, None
        self.x0, self.y0 = e.x_root, e.y_root
        if isinstance(e.widget, (tk.Entry, tk.Text)):
            return
        self.owner = self.find_owner(e.widget)
        if self.owner:
            self.owner.drag_start(e.y_root)

    def motion(self, e):
        if not self.moved and (abs(e.y_root - self.y0) > sz(10) or abs(e.x_root - self.x0) > sz(10)):
            self.moved = True
        if self.moved and self.owner:
            self.owner.drag_to(e.y_root)

    def wheel(self, e, d):
        try:
            o = self.find_owner(self.root.winfo_containing(e.x_root, e.y_root))
        except Exception:
            o = None
        if o:
            o.cv.yview_scroll(d * 3, "units")


def bind_tap(widget, cb):
    def rel(_e):
        if not TOUCH.moved:
            widget.after(1, cb)
    stack = [widget]
    while stack:
        w = stack.pop()
        w.bind("<ButtonRelease-1>", rel, add="+")
        stack.extend(w.winfo_children())


def rrect(cv, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2, x2 - r, y2,
           x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return cv.create_polygon(pts, smooth=True, **kw)


def draw_icon(cv, name, cx, cy, s, fg, bg):
    k = s / 24.0
    ox, oy = cx - s / 2.0, cy - s / 2.0

    def P(*a):
        return [(ox + v * k) if i % 2 == 0 else (oy + v * k) for i, v in enumerate(a)]
    lw = max(2, int(round(2.2 * k)))

    def L(pts):
        cv.create_line(*P(*pts), width=lw, fill=fg, capstyle="round", joinstyle="round")
    if name == "plus":
        L([12, 5, 12, 19]); L([5, 12, 19, 12])
    elif name == "minus":
        L([5, 12, 19, 12])
    elif name == "close":
        L([6, 6, 18, 18]); L([18, 6, 6, 18])
    elif name == "up":
        L([12, 19, 12, 5]); L([6, 11, 12, 5, 18, 11])
    elif name == "backup":
        L([12, 16, 12, 4]); L([6, 10, 12, 4, 18, 10]); L([4, 15, 4, 20, 20, 20, 20, 15])
    elif name == "restore":
        L([12, 4, 12, 16]); L([6, 10, 12, 16, 18, 10]); L([4, 15, 4, 20, 20, 20, 20, 15])
    elif name == "trash":
        L([4, 6, 20, 6]); L([9, 6, 9, 3, 15, 3, 15, 6])
        cv.create_polygon(P(6, 8, 18, 8, 17, 21, 7, 21), outline=fg, fill="", width=lw)
    elif name == "edit":
        L([4, 20, 5, 15, 16, 4, 20, 8, 9, 19, 4, 20])
    elif name == "folder":
        cv.create_polygon(P(2, 6, 9, 6, 11, 8, 22, 8, 22, 19, 2, 19), outline=fg, fill="", width=lw)
    elif name == "home":
        L([3, 12, 12, 4, 21, 12]); L([6, 10, 6, 20, 18, 20, 18, 10])
    elif name == "file":
        L([6, 3, 14, 3, 18, 7, 18, 21, 6, 21, 6, 3]); L([14, 3, 14, 7, 18, 7])
    elif name == "sd":
        L([6, 3, 18, 3, 18, 21, 6, 21, 6, 8, 11, 3]); L([10, 7, 10, 10]); L([13, 7, 13, 10])
    elif name == "gear":
        pts = []
        for i in range(32):
            a = i * math.pi / 16
            r = 10 if (i // 2) % 2 == 0 else 7.6
            pts += [12 + r * math.cos(a), 12 + r * math.sin(a)]
        cv.create_polygon(P(*pts), fill=fg, outline=fg)
        cv.create_oval(*P(8.2, 8.2, 15.8, 15.8), fill=bg, outline=bg)
    elif name == "pin":
        L([9, 3, 15, 3]); L([10, 3, 10, 10, 6, 14, 18, 14, 14, 10, 14, 3]); L([12, 14, 12, 21])
    elif name == "clock":
        cv.create_oval(*P(3, 3, 21, 21), outline=fg, width=lw)
        L([12, 7, 12, 12, 16, 14])
    elif name == "help":
        cv.create_oval(*P(3, 3, 21, 21), outline=fg, width=lw)
        cv.create_text(ox + 12 * k, oy + 12.5 * k, text="?", fill=fg,
                       font=(FONT["ui"], -int(14 * k), "bold"))


class Btn(tk.Canvas):
    """Botão arredondado, grande o bastante para toque."""

    def __init__(self, parent, text="", command=None, kind="ghost", icon=None,
                 w=None, h=None, minw=0, size=16):
        self.text, self.command, self.kind, self.icon = text, command, kind, icon
        self.font = F(size, True)
        h = h or sz(52)
        tw = tkfont.Font(font=self.font).measure(text) if text else 0
        iw = sz(24) if icon else 0
        gap = sz(10) if (icon and text) else 0
        self.cw = max(w or (sz(40) + iw + gap + tw + sz(6)), minw)
        self.ch = h
        self._cw_content = iw + gap + tw
        self.iw = iw
        self.gap = gap
        self.pressed = False
        self.disabled = False
        super().__init__(parent, width=self.cw, height=h, bg=parent.cget("bg"),
                         highlightthickness=0, bd=0, cursor="hand2")
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<ButtonRelease-1>", self._release)
        self.bind("<Leave>", self._leave)
        self.bind("<Configure>", self._resize)
        self.draw()

    def _resize(self, e):
        if e.width != self.cw or e.height != self.ch:
            self.cw, self.ch = e.width, e.height
            self.draw()

    def _colors(self):
        k = self.kind
        if self.disabled:
            return C["head"], C["mut"], C["line"]
        if k == "primary":
            f, fg, o = C["acc"], C["accink"], C["acc"]
        elif k == "danger":
            f, fg, o = C["dangerbg"], "#ffffff", C["dangerbg"]
        elif k == "hdr":
            f, fg, o = C["hdrbtn"], "#ffffff", "#6a4560"
        else:
            f, fg, o = C["head"], C["ink"], C["line"]
        if self.pressed:
            f = C["hdrpress"] if k == "hdr" else shade(f, 0.82)
        return f, fg, o

    def draw(self):
        self.delete("all")
        f, fg, o = self._colors()
        rrect(self, 1, 1, self.cw - 1, self.ch - 1, sz(9), fill=f, outline=o, width=1)
        x = (self.cw - self._cw_content) / 2.0
        if self.icon:
            draw_icon(self, self.icon, x + self.iw / 2.0, self.ch / 2.0, self.iw, fg, f)
            x += self.iw + self.gap
        if self.text:
            self.create_text(x, self.ch / 2.0, text=self.text, anchor="w", fill=fg, font=self.font)

    def set_disabled(self, v):
        self.disabled = v
        self.draw()

    def _press(self, _e):
        if not self.disabled:
            self.pressed = True
            self.draw()

    def _leave(self, _e):
        if self.pressed:
            self.pressed = False
            self.draw()

    def _release(self, e):
        was = self.pressed
        self.pressed = False
        self.draw()
        if was and 0 <= e.x <= self.cw and 0 <= e.y <= self.ch and not TOUCH.moved and self.command:
            self.after(1, self.command)


class Seg(tk.Frame):
    """Escolha segmentada (substitui o <select> do HTML, melhor para toque)."""

    def __init__(self, parent, options, value, command=None, minw=110):
        super().__init__(parent, bg=parent.cget("bg"))
        self.options, self.value, self.cmd, self.minw = options, value, command, minw
        self.render()

    def render(self):
        for c in self.winfo_children():
            c.destroy()
        for v, lab in self.options:
            Btn(self, lab, lambda v=v: self.set(v), "primary" if v == self.value else "ghost",
                minw=sz(self.minw)).pack(side="left", padx=(0, sz(6)))

    def set(self, v):
        self.value = v
        self.render()
        if self.cmd:
            self.cmd(v)


class Stepper(tk.Frame):
    def __init__(self, parent, value, lo, hi, step=1, fmt=str, command=None):
        super().__init__(parent, bg=parent.cget("bg"))
        self.value, self.lo, self.hi, self.step, self.fmt, self.cmd = value, lo, hi, step, fmt, command
        Btn(self, "", lambda: self.bump(-1), icon="minus", w=sz(56)).pack(side="left")
        self.lbl = tk.Label(self, font=F(18, True), fg=C["ink"], bg=parent.cget("bg"), width=9)
        self.lbl.pack(side="left")
        Btn(self, "", lambda: self.bump(1), icon="plus", w=sz(56)).pack(side="left")
        self.lbl.config(text=fmt(value))

    def bump(self, d):
        self.set(self.value + d * self.step)

    def set(self, v):
        self.value = min(self.hi, max(self.lo, v))
        self.lbl.config(text=self.fmt(self.value))
        if self.cmd:
            self.cmd(self.value)


class ScrollFrame(tk.Frame):
    """Área rolável por arraste de dedo, com indicador de posição discreto."""

    def __init__(self, parent, bg=None, height=None):
        bg = bg or C["bg"]
        super().__init__(parent, bg=bg)
        self._is_scroll = True
        self.cv = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        if height:
            self.cv.configure(height=height)
        self.bar = tk.Canvas(self, width=sz(8), height=1, bg=bg, highlightthickness=0, bd=0)
        self.bar.pack(side="right", fill="y", padx=(sz(4), 0))
        self.cv.pack(side="left", fill="both", expand=True)
        self.cv.configure(yscrollcommand=self._set)
        self.inner = tk.Frame(self.cv, bg=bg)
        self.win = self.cv.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.cv.configure(scrollregion=self.cv.bbox("all")))
        self.cv.bind("<Configure>", lambda e: self.cv.itemconfigure(self.win, width=e.width))
        self._y0 = 0
        self._top = 0

    def _set(self, a, b):
        a, b = float(a), float(b)
        self.bar.delete("all")
        if b - a >= 0.999:
            return
        h = self.bar.winfo_height()
        y1, y2 = a * h, max(b * h, a * h + sz(36))
        self.bar.create_rectangle(0, y1, sz(8), y2, fill=C["line"], outline=C["line"])

    def drag_start(self, y):
        self._y0, self._top = y, self.cv.canvasy(0)

    def drag_to(self, y):
        bb = self.cv.bbox("all")
        if not bb:
            return
        H, vh = bb[3], self.cv.winfo_height()
        if H <= vh:
            return
        self.cv.yview_moveto(min(max(self._top - (y - self._y0), 0), H - vh) / float(H))

    def clear(self):
        for c in self.inner.winfo_children():
            c.destroy()
        self.cv.yview_moveto(0)


def L(parent, text="", size=14, color="ink", bold=False, mono=False, **kw):
    kw.setdefault("bg", parent.cget("bg"))
    return tk.Label(parent, text=text, font=F(size, bold, mono), fg=C.get(color, color), **kw)


def mk_entry(parent, var):
    return tk.Entry(parent, textvariable=var, font=F(16, mono=True), bg=C["bg"], fg=C["ink"],
                    insertbackground=C["ink"], insertwidth=2, relief="flat", bd=sz(9),
                    highlightthickness=2, highlightbackground=C["line"], highlightcolor=C["acc"])


# =============================================================================
#  POPUPS (overlay dentro da janela — funciona bem no Gamescope/tela cheia)
# =============================================================================
class Modal:
    stack = []

    def __init__(self, app, title, width=560, height=None, dismiss=False, closable=True):
        self.app, self.closable, self.on_close = app, closable, None
        self.on_esc = self.is_dirty = None   # Esc / “há alterações?” (usados pelo editor de notas)
        self.ov = tk.Frame(app.root, bg=C["overlay"])
        self.ov.place(x=0, y=0, relwidth=1, relheight=1)
        self.card = tk.Frame(self.ov, bg=C["card"], highlightthickness=1, highlightbackground=C["line"])
        kw = dict(width=sz(width))
        if height:
            kw["height"] = sz(height)
            self.card.pack_propagate(False)
        self.card.place(relx=.5, rely=.5, anchor="center", **kw)
        tk.Frame(self.card, bg=C["acc"], height=3).pack(fill="x")
        tk.Label(self.card, text=title, font=F(24, True), fg=C["acc"], bg=C["card"]).pack(pady=(sz(14), sz(6)))
        self.foot = tk.Frame(self.card, bg=C["card"])
        self.foot.pack(side="bottom", fill="x", padx=sz(24), pady=sz(14))
        self.body = tk.Frame(self.card, bg=C["card"])
        self.body.pack(fill="both", expand=True, padx=sz(24))
        if dismiss:
            self.ov.bind("<ButtonRelease-1>", lambda e: e.widget is self.ov and not TOUCH.moved and self.close())
        Modal.stack.append(self)

    def buttons(self, specs):
        for text, cmd, kind in reversed(specs):
            Btn(self.foot, text, cmd, kind, minw=sz(170)).pack(side="right", padx=(sz(10), 0))

    def close(self):
        if self in Modal.stack:
            Modal.stack.remove(self)
        try:
            self.ov.destroy()
        except tk.TclError:
            pass
        if self.on_close:
            self.on_close()


def confirm(app, title, message, ok_text, on_ok, danger=False):
    m = Modal(app, title, width=640)
    tk.Label(m.body, text=message, font=F(15), fg=C["ink"], bg=C["card"], justify="left",
             wraplength=sz(590), anchor="w").pack(fill="x", pady=sz(8))
    m.buttons([("Cancelar", m.close, "ghost"),
               (ok_text, lambda: (m.close(), on_ok()), "danger" if danger else "primary")])


def info_box(app, title, message):
    m = Modal(app, title, width=640)
    tk.Label(m.body, text=message, font=F(15), fg=C["ink"], bg=C["card"], justify="left",
             wraplength=sz(590), anchor="w").pack(fill="x", pady=sz(8))
    m.buttons([("Fechar", m.close, "primary")])


class FolderPicker:
    """Seletor de pastas/arquivos próprio (o diálogo nativo do Tk é ruim para toque)."""

    def __init__(self, app, start, on_pick, title="Escolher pasta", ext=None, ok_text="Selecionar esta pasta"):
        # ext (ex.: ".json"): modo ARQUIVO — só lista arquivos dessa extensão e um toque seleciona.
        self.app, self.on_pick, self.ext = app, on_pick, ext
        self.hidden = False
        p = Path(start or Path.home())
        while not p.is_dir() and p != p.parent:
            p = p.parent
        self.path = p
        self.m = Modal(app, title, width=980, height=700)
        top = tk.Frame(self.m.body, bg=C["card"])
        top.pack(fill="x")
        self.lbl = L(top, "", 14, "ink", mono=True, anchor="w", bg=C["card"])
        self.lbl.pack(fill="x", pady=(0, sz(8)))
        bar = tk.Frame(self.m.body, bg=C["card"])
        bar.pack(fill="x", pady=(0, sz(8)))
        Btn(bar, "Subir", self.up, icon="up").pack(side="left", padx=(0, sz(6)))
        Btn(bar, "Início", lambda: self.go(Path.home()), icon="home").pack(side="left", padx=(0, sz(6)))
        sd = self._sd()
        if sd:
            Btn(bar, "Cartão SD", lambda: self.go(sd), icon="sd").pack(side="left", padx=(0, sz(6)))
        self.hbtn = Btn(bar, "Ocultas: não", self.toggle_hidden)
        self.hbtn.pack(side="right")
        self.sf = ScrollFrame(self.m.body, bg=C["bg"])
        self.sf.pack(fill="both", expand=True)
        specs = [("Cancelar", self.m.close, "ghost")]
        if not ext:
            specs.append((ok_text, self.pick, "primary"))
        self.m.buttons(specs)
        self.refresh()

    @staticmethod
    def _sd():
        base = Path("/run/media")
        try:
            for u in sorted(base.iterdir()):
                for d in sorted(u.iterdir()) if u.is_dir() and u.name != "deck" else []:
                    if d.is_dir():
                        return d
                if u.is_dir() and u.name == "deck":
                    for d in sorted(u.iterdir()):
                        if d.is_dir():
                            return d
                if u.is_dir() and any(True for _ in u.iterdir()):
                    return u
        except OSError:
            pass
        return None

    def toggle_hidden(self):
        self.hidden = not self.hidden
        self.hbtn.destroy()
        self.hbtn = Btn(self.hbtn.master, "Ocultas: sim" if self.hidden else "Ocultas: não", self.toggle_hidden)
        self.hbtn.pack(side="right")
        self.refresh()

    def go(self, p):
        if Path(p).is_dir():
            self.path = Path(p)
            self.refresh()

    def up(self):
        self.go(self.path.parent)

    def pick(self):
        self.m.close()
        self.on_pick(str(self.path))

    def pick_file(self, name):
        self.m.close()
        self.on_pick(str(self.path / name))

    def row(self, text, icon, cb, dim=False):
        f = tk.Frame(self.sf.inner, bg=C["card"], highlightthickness=1, highlightbackground=C["line"])
        f.pack(fill="x", pady=(0, sz(4)), padx=(0, sz(6)))
        cv = tk.Canvas(f, width=sz(40), height=sz(52), bg=C["card"], highlightthickness=0)
        cv.pack(side="left", padx=(sz(8), 0))
        draw_icon(cv, icon, sz(20), sz(26), sz(24), C["mut"] if dim else C["acc"], C["card"])
        tk.Label(f, text=text, font=F(16, mono=True), fg=C["mut"] if dim else C["ink"], bg=C["card"],
                 anchor="w").pack(side="left", fill="x", expand=True, padx=sz(8), pady=sz(8))
        if cb:
            bind_tap(f, cb)

    def refresh(self):
        self.lbl.config(text=str(self.path))
        self.sf.clear()
        dirs, files = [], []
        try:
            for e in os.scandir(self.path):
                if e.name.startswith(".") and not self.hidden:
                    continue
                try:
                    (dirs if e.is_dir() else files).append(e.name)
                except OSError:
                    pass
        except OSError:
            L(self.sf.inner, "Sem permissão para ler esta pasta.", 15, "danger").pack(pady=sz(20))
            return
        dirs.sort(key=str.lower)
        files.sort(key=str.lower)
        if self.ext:
            files = [n for n in files if n.lower().endswith(self.ext)]
        for n in dirs[:300]:
            self.row(n, "folder", lambda n=n: self.go(self.path / n))
        for n in files[:300 if self.ext else 60]:
            self.row(n, "file", (lambda n=n: self.pick_file(n)) if self.ext else None, dim=not self.ext)
        if len(dirs) > 300:
            L(self.sf.inner, "… mais de 300 pastas; navegue por um nível acima.", 13, "mut").pack(pady=sz(8))
        if not dirs and not files:
            L(self.sf.inner, "Pasta vazia.", 15, "mut").pack(pady=sz(20))
        elif self.ext and not files:
            L(self.sf.inner, "Nenhum arquivo %s nesta pasta." % self.ext, 14, "mut").pack(pady=sz(12))


# =============================================================================
#  CAMPO DE TEXTO COM BARRA DE ARRASTE (o Text do Tk não rola com o dedo)
# =============================================================================
class TextScroll(tk.Frame):
    """Text com uma faixa lateral grande: arraste nela para rolar a nota."""

    def __init__(self, parent, lines, px):
        super().__init__(parent, bg=C["bg"], highlightthickness=2, highlightbackground=C["line"],
                         highlightcolor=C["line"])
        self.bar = tk.Canvas(self, width=sz(34), bg=C["bg"], highlightthickness=0, bd=0, cursor="hand2")
        self.bar.pack(side="right", fill="y")
        self.t = tk.Text(self, wrap="word", undo=True, maxundo=300, height=lines, font=F(px),
                         bg=C["bg"], fg=C["ink"], insertbackground=C["ink"], insertwidth=2,
                         selectbackground=C["acc"], selectforeground=C["accink"], relief="flat",
                         bd=sz(9), highlightthickness=0, padx=sz(4), pady=sz(2), yscrollcommand=self._set)
        self.t.pack(side="left", fill="both", expand=True)
        for ev in ("<ButtonPress-1>", "<B1-Motion>"):
            self.bar.bind(ev, self._drag)
        self.bar.bind("<Configure>", lambda e: self._set(*self.t.yview()))
        self.t.bind("<FocusIn>", lambda e: self._border(C["acc"]))
        self.t.bind("<FocusOut>", lambda e: self._border(C["line"]))

    def _border(self, color):
        self.config(highlightbackground=color, highlightcolor=color)

    def _set(self, a, b):
        a, b = float(a), float(b)
        self.bar.delete("all")
        if b - a >= 0.999:
            return
        h = self.bar.winfo_height()
        y1, y2 = a * h, max(b * h, a * h + sz(40))
        rrect(self.bar, sz(8), y1 + 1, sz(26), y2 - 1, sz(6), fill=C["line"], outline=C["line"])

    def _drag(self, e):
        h = max(1, self.bar.winfo_height())
        a, b = self.t.yview()
        self.t.yview_moveto(min(1.0, max(0.0, e.y / float(h))) - (b - a) / 2.0)


# =============================================================================
#  JOGO: ESCOLHER / CRIAR
# =============================================================================
def choose_game(app, current, on_pick):
    m = Modal(app, "Escolher jogo", width=640, height=560)
    sf = ScrollFrame(m.body, bg=C["bg"])
    sf.pack(fill="both", expand=True)

    def pick(gid):
        m.close()
        on_pick(gid)
    for g in app.store["games"]:
        sel = g["id"] == current
        bg = C["sel"] if sel else C["card"]
        f = tk.Frame(sf.inner, bg=bg, highlightthickness=sz(2) if sel else 1,
                     highlightbackground=C["acc"] if sel else C["line"])
        f.pack(fill="x", pady=(0, sz(6)), padx=(0, sz(6)))
        tk.Label(f, text=g["name"], font=F(17, True), fg=C["ink"], bg=bg, anchor="w",
                 wraplength=sz(520), justify="left").pack(fill="x", padx=sz(14), pady=sz(12))
        bind_tap(f, lambda gid=g["id"]: pick(gid))
    m.buttons([("Cancelar", m.close, "ghost"),
               ("Novo jogo", lambda: (m.close(), app.game_dialog(None, lambda g: on_pick(g["id"]))), "primary")])


# =============================================================================
#  EDITOR DE NOTA
# =============================================================================
class NoteEditor:
    def __init__(self, app, note=None, game_id=None):
        self.app, self.orig = app, note
        n = dict(note) if note else dict(id=new_id(), game=game_id, title="", text="", created=now_iso(),
                                         modified=now_iso(), pinned=False, color="none")
        self.n = n
        m = self.m = Modal(app, "Editar nota" if note else "Nova nota", width=1120)
        # Ancorado no topo: o teclado virtual do Steam Deck cobre a metade de baixo da tela.
        m.card.place_configure(rely=0, y=sz(10), anchor="n")
        m.on_esc, m.is_dirty = self.cancel, self.dirty
        card = C["card"]

        L(m.body, "Título (opcional)", 13, "mut", anchor="w").pack(fill="x")
        self.tv = tk.StringVar(value=n["title"])
        te = mk_entry(m.body, self.tv)
        te.config(font=F(16, True))
        te.pack(fill="x", ipady=sz(1), pady=(sz(2), sz(8)))

        row = tk.Frame(m.body, bg=card)
        row.pack(fill="x")
        L(row, "Nota", 13, "mut", anchor="w").pack(side="left")
        self.cnt = L(row, "", 13, "mut", anchor="e")
        self.cnt.pack(side="right")
        self.ts = TextScroll(m.body, 8, app.cfg["settings"]["font_size"])
        self.ts.pack(fill="x", pady=(sz(2), sz(8)))
        self.t = self.ts.t
        self.t.insert("1.0", n["text"])
        self.t.edit_reset()
        self.t.bind("<KeyRelease>", lambda e: self.count())
        self.t.bind("<ButtonRelease-1>", lambda e: self.count())

        self.bar = tk.Frame(m.body, bg=card)
        self.bar.pack(fill="x")
        self.render_bar()
        self.err = L(m.body, "Dica: o teclado virtual abre com Steam + X.", 13, "mut", anchor="w",
                     wraplength=sz(1060), justify="left")
        self.err.pack(fill="x", pady=(sz(8), 0))

        if note:
            Btn(m.foot, "Excluir", self.delete, "danger", minw=sz(170)).pack(side="left")
        m.buttons([("Cancelar", self.cancel, "ghost"), ("Salvar", self.save, "primary")])
        for w in (te, self.t):
            w.bind("<Control-s>", lambda e: (self.save(), "break")[1])
        self.count()
        self.snap = self.state()
        (self.t if note else te).focus_set()

    # ---- estado ---------------------------------------------------------------
    def state(self):
        n = self.n
        return (self.tv.get().strip(), self.t.get("1.0", "end-1c").strip(), n["color"], n["pinned"], n["game"])

    def dirty(self):
        return self.state() != self.snap

    def count(self):
        txt = self.t.get("1.0", "end-1c")
        self.cnt.config(text="%d caracteres  •  %d linhas" % (len(txt), txt.count("\n") + 1 if txt else 0))

    # ---- barra de controles -------------------------------------------------------
    def render_bar(self):
        for c in self.bar.winfo_children():
            c.destroy()
        n, card = self.n, C["card"]
        g = self.app.game(n["game"])
        name = g["name"] if g else "—"
        Btn(self.bar, "Jogo: " + (name if len(name) <= 22 else name[:21] + "…"), self.pick_game,
            icon="folder").pack(side="left")
        sw = tk.Frame(self.bar, bg=card)
        sw.pack(side="left", padx=(sz(16), 0))
        for key, hexc in NOTE_COLORS:
            cv = tk.Canvas(sw, width=sz(38), height=sz(38), bg=hexc or card, bd=0, cursor="hand2",
                           highlightthickness=2,
                           highlightbackground=C["acc"] if key == n["color"] else C["line"])
            if hexc is None:
                cv.create_line(sz(8), sz(30), sz(30), sz(8), fill=C["mut"], width=2)
            cv.pack(side="left", padx=(0, sz(6)))
            cv.bind("<ButtonRelease-1>", lambda e, k=key: not TOUCH.moved and self.set_color(k))
        Btn(self.bar, "Fixada" if n["pinned"] else "Fixar", self.toggle_pin,
            "primary" if n["pinned"] else "ghost", icon="pin").pack(side="right")
        Btn(self.bar, "Hora", self.insert_time, icon="clock").pack(side="right", padx=(0, sz(8)))

    def set_color(self, key):
        self.n["color"] = key
        self.render_bar()

    def toggle_pin(self):
        self.n["pinned"] = not self.n["pinned"]
        self.render_bar()

    def pick_game(self):
        def got(gid):
            self.n["game"] = gid
            self.render_bar()
        choose_game(self.app, self.n["game"], got)

    def insert_time(self):
        """Insere “[HH:MM] ” numa linha nova (ou no começo da linha vazia onde está o cursor)."""
        t = self.t
        if t.get("insert linestart", "insert lineend").strip():
            t.mark_set("insert", "insert lineend")
            t.insert("insert", "\n")
        t.insert("insert", datetime.now().strftime("[%H:%M] "))
        t.see("insert")
        t.focus_set()
        self.count()

    # ---- ações ---------------------------------------------------------------------
    def cancel(self):
        if self.dirty():
            confirm(self.app, "Descartar alterações", "Há alterações não salvas nesta nota.\n\nDescartar?",
                    "Descartar", self.m.close, danger=True)
        else:
            self.m.close()

    def save(self):
        title, text = self.state()[:2]
        if not title and not text:
            self.err.config(text="⚠ A nota está vazia.", fg=C["danger"])
            return
        if self.orig and not self.dirty():
            self.m.close()
            return
        self.n.update(title=title, text=text, modified=now_iso())
        self.m.close()
        self.app.save_note(self.n, self.orig)

    def delete(self):
        def go():
            self.m.close()
            self.app.delete_note(self.orig)
        confirm(self.app, "Excluir nota", "Excluir a nota “%s”?\n\nEsta ação não pode ser desfeita."
                % note_heading(self.orig), "Excluir", go, danger=True)


# =============================================================================
#  APLICATIVO
# =============================================================================
class App:
    def __init__(self):
        global TOUCH
        self.cfg = load_config()
        self.store = load_store()
        self.root = tk.Tk()
        self.root.title("%s v%s" % (APP_NAME, APP_VER))
        self.root.geometry("1280x800")
        fams = set(tkfont.families())
        FONT["ui"] = next((f for f in ("Ubuntu", "Cantarell", "Noto Sans", "DejaVu Sans") if f in fams), "sans-serif")
        FONT["mono"] = next((f for f in ("Ubuntu Mono", "DejaVu Sans Mono", "Consolas", "Cascadia Mono", "Liberation Mono",
                                         "Noto Sans Mono", "Courier New") if f in fams),
                            tkfont.nametofont("TkFixedFont").actual("family"))
        TOUCH = Touch(self.root)
        st = self.cfg["settings"]
        self.sel = st.get("last_game") if self.game(st.get("last_game")) else None   # None = todas
        self.qv = tk.StringVar()
        self._qjob = None
        self.qv.trace_add("write", self._q_changed)
        self.main = self.nsf = self.nlbl = None
        self.apply_look()
        self.build_ui()
        self.root.bind("<Escape>", self.on_escape)
        self.root.protocol("WM_DELETE_WINDOW", self.quit_app)
        self.root.bind("<F11>", lambda e: self.toggle_fullscreen())
        self.root.bind("<Control-n>", lambda e: not Modal.stack and self.new_note())
        if self.store.get("recovered"):
            self.root.after(400, lambda: info_box(
                self, "Arquivo de notas ilegível",
                "O arquivo de notas não pôde ser lido e foi guardado em:\n%s\n\nComeçando com uma lista vazia." % self.store["recovered"]))

    # ---- aparência -------------------------------------------------------------
    def apply_look(self):
        global S
        st = self.cfg["settings"]
        C.clear()
        C.update(THEMES.get(st["theme"], THEMES["dark"]))
        S = max(0.7, min(1.6, st.get("scale", 100) / 100.0))
        self.root.configure(bg=C["bg"])
        self.root.attributes("-fullscreen", bool(st["fullscreen"]))

    def on_escape(self, _e):
        if Modal.stack and Modal.stack[-1].closable:
            m = Modal.stack[-1]
            (m.on_esc or m.close)()

    def quit_app(self):
        if any(not m.closable for m in Modal.stack):
            return
        if any(m.is_dirty and m.is_dirty() for m in Modal.stack):
            self.set_status("Salve ou descarte a nota aberta antes de sair.", "danger")
            return
        save_config(self.cfg)
        self.root.destroy()

    def toggle_fullscreen(self):
        st = self.cfg["settings"]
        st["fullscreen"] = not st["fullscreen"]
        self.root.attributes("-fullscreen", st["fullscreen"])
        save_config(self.cfg)

    def build_ui(self):
        if self.main:
            self.main.destroy()
        self.nsf = self.nlbl = None
        self.main = tk.Frame(self.root, bg=C["bg"])
        self.main.pack(fill="both", expand=True)
        hd = tk.Frame(self.main, bg=C["bar"])
        hd.pack(fill="x")
        tk.Frame(self.main, bg=C["acc"], height=3).pack(fill="x")
        pad = tk.Frame(hd, bg=C["bar"])
        pad.pack(fill="x", padx=sz(14), pady=sz(10))
        Btn(pad, "Nova nota", self.new_note, "hdr", icon="plus").pack(side="left", padx=(0, sz(10)))
        tk.Frame(pad, bg="#6a4560", width=1, height=sz(34)).pack(side="left", padx=sz(6))
        Btn(pad, "", self.open_settings, "hdr", icon="gear", w=sz(56)).pack(side="left", padx=(sz(6), sz(8)))
        Btn(pad, "", self.open_help, "hdr", icon="help", w=sz(56)).pack(side="left")
        Btn(pad, "", self.quit_app, "hdr", icon="close", w=sz(56)).pack(side="right", padx=(sz(12), 0))
        self.build_title(pad)

        self.foot = tk.Frame(self.main, bg=C["head"], highlightthickness=1, highlightbackground=C["line"])
        self.foot.pack(side="bottom", fill="x")
        self.info_lbl = tk.Label(self.foot, text="", font=F(13), fg=C["mut"], bg=C["head"], anchor="e")
        self.info_lbl.pack(side="right", padx=(sz(10), sz(14)), pady=sz(5))
        self.status = tk.Label(self.foot, text="Pronto.", font=F(13), fg=C["mut"], bg=C["head"], anchor="w")
        self.status.pack(side="left", fill="x", expand=True, padx=(sz(14), 0), pady=sz(5))

        body = tk.Frame(self.main, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=sz(14), pady=sz(12))
        left = tk.Frame(body, bg=C["bg"], width=sz(390))
        left.pack(side="left", fill="y", padx=(0, sz(14)))
        left.pack_propagate(False)
        L(left, "JOGOS", 13, "acc", bold=True, anchor="w").pack(fill="x", pady=(0, sz(6)))
        Btn(left, "Novo jogo", lambda: self.game_dialog(None), icon="plus").pack(side="bottom", fill="x",
                                                                                pady=(sz(8), 0))
        self.plist = ScrollFrame(left)
        self.plist.pack(fill="both", expand=True)
        self.detail = tk.Frame(body, bg=C["bg"])
        self.detail.pack(side="left", fill="both", expand=True)
        self.refresh()

    def build_title(self, parent):
        """Título: “µ” em itálico e “Notas”, na mesma linha de base."""
        big = F(34)
        parts = [("µ", (big[0], big[1], "italic")), ("Notas", big)]
        fonts = [tkfont.Font(font=f) for _t, f in parts]
        main = fonts[1]
        asc, h = main.metrics("ascent"), main.metrics("linespace")
        w = sum(f.measure(t) for (t, _f), f in zip(parts, fonts)) + sz(2)
        cv = tk.Canvas(parent, width=w, height=h, bg=C["bar"], highlightthickness=0, bd=0)
        cv.pack(side="right")
        x = 0
        for (t, f), fo in zip(parts, fonts):
            cv.create_text(x, asc + fo.metrics("descent"), text=t, font=f, fill="#ffffff", anchor="sw")
            x += fo.measure(t)

    def set_status(self, text, color="mut"):
        self.status.config(text=text, fg=C[color])

    # ---- dados -----------------------------------------------------------------
    def game(self, gid):
        return next((g for g in self.store["games"] if g["id"] == gid), None) if gid else None

    def commit(self):
        err = save_store(self.store)
        if err:
            self.set_status("Falha ao salvar as notas: %s" % err, "danger")
        return err is None

    def default_game(self):
        if self.sel:
            return self.sel
        last = self.cfg["settings"].get("last_note_game")
        return last if self.game(last) else ensure_game(self.store)

    def select(self, gid):
        self.sel = gid
        self.cfg["settings"]["last_game"] = gid
        self.qv.set("")
        self.refresh()

    def refresh(self):
        self.fill_games()
        self.fill_detail()
        st = self.store
        self.info_lbl.config(text="v%s  •  %d jogos  •  %d notas" % (APP_VER, len(st["games"]), len(st["notes"])))

    # ---- lista de jogos ----------------------------------------------------------
    def fill_games(self):
        sf = self.plist
        sf.clear()
        counts = collections.Counter(n["game"] for n in self.store["notes"])
        entries = [(None, "Todas as notas", len(self.store["notes"]))] + \
                  [(g["id"], g["name"], counts.get(g["id"], 0)) for g in self.store["games"]]
        for gid, name, cnt in entries:
            sel = gid == self.sel
            bg = C["sel"] if sel else C["card"]
            f = tk.Frame(sf.inner, bg=bg, highlightthickness=sz(2) if sel else 1,
                         highlightbackground=C["acc"] if sel else C["line"])
            f.pack(fill="x", pady=(0, sz(8)), padx=(0, sz(6)))
            tk.Label(f, text=name, font=F(19, True), fg=C["ink"], bg=bg, anchor="w",
                     wraplength=sz(340), justify="left").pack(fill="x", padx=sz(14), pady=(sz(10), 0))
            tk.Label(f, text="%d nota%s" % (cnt, "" if cnt == 1 else "s"), font=F(13),
                     fg=C["acc"] if cnt else C["mut"], bg=bg, anchor="w").pack(fill="x", padx=sz(14), pady=(0, sz(10)))
            bind_tap(f, lambda gid=gid: self.select(gid))

    # ---- painel de notas -----------------------------------------------------------
    def fill_detail(self):
        d = self.detail
        for c in d.winfo_children():
            c.destroy()
        g = self.game(self.sel)
        top = tk.Frame(d, bg=C["bg"])
        top.pack(fill="x")
        if g:
            Btn(top, "Exportar", self.export_game, icon="backup").pack(side="right", padx=(sz(8), 0))
            Btn(top, "Editar", lambda: self.game_dialog(g), icon="edit").pack(side="right")
        L(top, g["name"] if g else "Todas as notas", 28, "acc", bold=True, anchor="w",
          wraplength=sz(560), justify="left").pack(side="left", fill="x", expand=True)
        Btn(d, "Nova nota", self.new_note, "primary", icon="plus", h=sz(64), size=20).pack(
            fill="x", pady=(sz(10), sz(10)))

        L(d, "Buscar nas notas", 13, "mut", anchor="w").pack(fill="x")
        row = tk.Frame(d, bg=C["bg"])
        row.pack(fill="x", pady=(sz(2), 0))
        Btn(row, "", lambda: self.qv.set(""), icon="close", w=sz(56)).pack(side="right", padx=(sz(6), 0))
        e = mk_entry(row, self.qv)
        e.config(font=F(16))
        e.pack(side="left", fill="x", expand=True, ipady=sz(1))
        self.root.bind("<Control-f>", lambda ev: e.focus_set())

        self.nlbl = L(d, "", 13, "acc", bold=True, anchor="w")
        self.nlbl.pack(fill="x", pady=(sz(12), sz(6)))
        self.nsf = ScrollFrame(d)
        self.nsf.pack(fill="both", expand=True)
        self.fill_notes()

    def _q_changed(self, *_a):
        if self._qjob:
            self.root.after_cancel(self._qjob)
        self._qjob = self.root.after(200, self.fill_notes)

    def fill_notes(self):
        self._qjob = None
        if not self.nsf or not self.nsf.winfo_exists():
            return
        q = self.qv.get().strip()
        notes = visible_notes(self.store, self.sel, q)
        self.nlbl.config(text=("RESULTADOS (%d)" if q else "NOTAS (%d)") % len(notes))
        sf = self.nsf
        sf.clear()
        if not notes:
            L(sf.inner, "Nada encontrado para a busca." if q else "Nenhuma nota ainda.\nToque em “Nova nota”.",
              15, "mut", justify="center").pack(pady=sz(30))
        for n in notes[:MAX_RENDER]:
            self.note_card(sf, n)
        if len(notes) > MAX_RENDER:
            L(sf.inner, "Mostrando %d de %d notas; use a busca para achar as outras." % (MAX_RENDER, len(notes)),
              13, "mut").pack(pady=sz(8))

    def note_card(self, sf, n):
        bg = C["card"]
        f = tk.Frame(sf.inner, bg=bg, highlightthickness=1, highlightbackground=C["line"])
        f.pack(fill="x", pady=(0, sz(6)), padx=(0, sz(6)))
        tk.Frame(f, bg=COLOR_MAP.get(n["color"]) or bg, width=sz(8)).pack(side="left", fill="y")
        if n["pinned"]:
            cv = tk.Canvas(f, width=sz(40), height=sz(40), bg=bg, highlightthickness=0)
            cv.pack(side="right", anchor="n", padx=sz(8), pady=sz(8))
            draw_icon(cv, "pin", sz(20), sz(20), sz(24), C["acc"], bg)
        body = tk.Frame(f, bg=bg)
        body.pack(side="left", fill="x", expand=True, padx=sz(12), pady=sz(8))
        tk.Label(body, text=note_heading(n), font=F(17, True), fg=C["ink"], bg=bg, anchor="w",
                 justify="left", wraplength=sz(680)).pack(fill="x")
        prev = note_preview(n)
        if prev:
            tk.Label(body, text=prev, font=F(14), fg=C["mut"], bg=bg, anchor="w", justify="left",
                     wraplength=sz(680)).pack(fill="x")
        meta = fmt_short(n["modified"])
        if self.sel is None:
            g = self.game(n["game"])
            meta += "  •  " + (g["name"] if g else "—")
        tk.Label(body, text=meta, font=F(12), fg=C["acc"], bg=bg, anchor="w").pack(fill="x", pady=(sz(2), 0))
        bind_tap(f, lambda n=n: NoteEditor(self, n))

    # ---- notas -----------------------------------------------------------------------
    def new_note(self):
        NoteEditor(self, None, self.default_game())

    def save_note(self, n, orig):
        notes = self.store["notes"]
        if orig:
            for i, x in enumerate(notes):
                if x["id"] == n["id"]:
                    notes[i] = n
        else:
            notes.append(n)
        self.cfg["settings"]["last_note_game"] = n["game"]
        ok = self.commit()
        save_config(self.cfg)
        self.refresh()
        g = self.game(n["game"])
        if ok:
            moved = self.sel and n["game"] != self.sel
            self.set_status(("Nota movida para “%s”." % g["name"]) if moved else "Nota salva.", "ok")

    def delete_note(self, n):
        self.store["notes"] = [x for x in self.store["notes"] if x["id"] != n["id"]]
        if self.commit():
            self.set_status("Nota excluída.")
        self.refresh()

    # ---- jogos -------------------------------------------------------------------------
    def game_dialog(self, game=None, on_saved=None):
        m = Modal(self, "Renomear jogo" if game else "Novo jogo", width=760)
        L(m.body, "Nome do jogo", 13, "mut", bg=C["card"], anchor="w").pack(fill="x")
        var = tk.StringVar(value=game["name"] if game else "")
        ent = mk_entry(m.body, var)
        ent.pack(fill="x", ipady=sz(1), pady=(sz(2), sz(8)))
        msg = L(m.body, "Dica: o teclado virtual abre com Steam + X.", 13, "mut", bg=C["card"], anchor="w")
        msg.pack(fill="x")

        def save():
            name = var.get().strip()
            if not name:
                msg.config(text="⚠ Informe o nome do jogo.", fg=C["danger"])
                return
            if any(g["id"] != (game or {}).get("id") and g["name"].casefold() == name.casefold()
                   for g in self.store["games"]):
                msg.config(text="⚠ Já existe um jogo com esse nome.", fg=C["danger"])
                return
            if game:
                game["name"] = name
                g = game
            else:
                g = dict(id=new_id(), name=name, created=now_iso())
                self.store["games"].append(g)
            self.commit()
            m.close()
            if game:
                self.refresh()
            else:
                (on_saved or (lambda g: self.select(g["id"])))(g)
                self.refresh()
            self.set_status("Jogo “%s” salvo." % name, "ok")
        ent.bind("<Return>", lambda e: save())
        if game:
            Btn(m.foot, "Excluir jogo", lambda: (m.close(), self.delete_game(game)), "danger",
                minw=sz(170)).pack(side="left")
        m.buttons([("Cancelar", m.close, "ghost"), ("Salvar", save, "primary")])
        ent.focus_set()
        ent.icursor("end")

    def delete_game(self, game):
        n = sum(1 for x in self.store["notes"] if x["game"] == game["id"])

        def go():
            self.store["notes"] = [x for x in self.store["notes"] if x["game"] != game["id"]]
            self.store["games"] = [g for g in self.store["games"] if g["id"] != game["id"]]
            ensure_game(self.store)
            self.sel = None
            self.cfg["settings"]["last_game"] = None
            self.commit()
            save_config(self.cfg)
            self.refresh()
            self.set_status("Jogo excluído.")
        msg = ("Excluir o jogo “%s” e %s?\n\nEsta ação não pode ser desfeita."
               % (game["name"], "a sua única nota" if n == 1 else "as suas %d notas" % n)) if n else \
              "Excluir o jogo “%s”?\n\nEle não tem notas." % game["name"]
        confirm(self, "Excluir jogo", msg, "Excluir", go, danger=True)

    # ---- exportar / importar ------------------------------------------------------------
    def export_game(self):
        g = self.game(self.sel)
        notes = [n for n in self.store["notes"] if g and n["game"] == g["id"]]
        if not notes:
            self.set_status("Este jogo ainda não tem notas para exportar.")
            return
        st = self.cfg["settings"]

        def got(folder):
            st["io_dir"] = folder
            path = Path(folder) / (safe_name(g["name"]) + ".md")

            def go():
                try:
                    write_text_atomic(path, game_markdown(g, notes))
                except OSError as e:
                    self.set_status("Não foi possível exportar: %s" % e, "danger")
                    return
                save_config(self.cfg)
                self.set_status("Exportado: %s" % path, "ok")
            if path.exists():
                confirm(self, "Substituir arquivo", "Já existe um arquivo com este nome:\n%s\n\nSubstituir?" % path,
                        "Substituir", go)
            else:
                go()
        FolderPicker(self, st.get("io_dir") or str(Path.home()), got, "Exportar notas — escolha a pasta",
                     ok_text="Exportar aqui")

    def export_all(self):
        st = self.cfg["settings"]

        def got(folder):
            st["io_dir"] = folder
            path = Path(folder) / ("unotas_%s%s" % (datetime.now().strftime("%Y%m%d-%H%M%S"), BACKUP_EXT))
            try:
                write_json_atomic(path, backup_data(self.store))
            except OSError as e:
                info_box(self, "Falha ao exportar", str(e))
                return
            save_config(self.cfg)
            info_box(self, "Backup exportado", "%d notas em %d jogos.\n\n%s" % (
                len(self.store["notes"]), len(self.store["games"]), path))
        FolderPicker(self, st.get("io_dir") or str(Path.home()), got, "Backup — escolha a pasta",
                     ok_text="Exportar aqui")

    def import_all(self):
        st = self.cfg["settings"]

        def got(path):
            st["io_dir"] = os.path.dirname(path)
            try:
                res = merge_backup(self.store, read_backup_file(path))
            except ValueError as e:
                info_box(self, "Falha ao importar", str(e))
                return
            self.commit()
            save_config(self.cfg)
            self.refresh()
            info_box(self, "Importação concluída", "Notas novas: %d\nNotas atualizadas: %d\nJogos novos: %d\n\n"
                     "Nada foi apagado: o backup foi mesclado às suas notas." % (res["new"], res["upd"], res["games"]))
        FolderPicker(self, st.get("io_dir") or str(Path.home()), got, "Importar backup — escolha o arquivo",
                     ext=".json")

    # ---- popups ----------------------------------------------------------------------------
    def open_help(self):
        m = Modal(self, "Ajuda", width=760, dismiss=True)
        txt = ("1. Crie um jogo em “Novo jogo” e toque em “Nova nota” para anotar. A nota é do jogo "
               "selecionado; em “Todas as notas” vale o último jogo usado.\n\n"
               "2. No editor, “Hora” insere [HH:MM] numa linha nova (bom para diário de bordo), "
               "“Fixar” mantém a nota no topo e as cores ajudam a separar assuntos.\n\n"
               "3. A busca olha título e texto. “Exportar” (ao lado do nome do jogo) gera um arquivo .md; "
               "em Configurações há o backup completo para levar tudo a outro dispositivo.\n\n"
               "• Arraste a faixa à direita do texto para rolar uma nota longa.\n"
               "• Teclado virtual: Steam + X.   Tela cheia: F11.   Salvar: Ctrl+S.\n"
               "• Dados: %s" % CFG_DIR)
        tk.Label(m.body, text=txt, font=F(15), fg=C["ink"], bg=C["card"], justify="left", anchor="w",
                 wraplength=sz(700)).pack(fill="x", pady=sz(6))
        tk.Label(m.body, text="%s v%s" % (APP_NAME, APP_VER), font=F(13), fg=C["mut"], bg=C["card"]).pack(pady=(sz(6), 0))
        m.buttons([("Fechar", m.close, "primary")])

    def open_settings(self):
        st = self.cfg["settings"]
        m = Modal(self, "Configurações", width=860, dismiss=False)
        g = tk.Frame(m.body, bg=C["card"])
        g.pack(fill="x")
        g.columnconfigure(1, weight=1)

        def row(i, label, widget_fn, hint=""):
            lf = tk.Frame(g, bg=C["card"])
            lf.grid(row=i, column=0, sticky="w", pady=sz(5), padx=(0, sz(20)))
            L(lf, label, 15, "ink", bold=True, anchor="w", width=22).pack(anchor="w")
            if hint:
                L(lf, hint, 12, "mut", anchor="w").pack(anchor="w")
            widget_fn(g).grid(row=i, column=1, sticky="w", pady=sz(5))
        yn = [(True, "Sim"), (False, "Não")]
        row(0, "Tema", lambda p: Seg(p, [("dark", "Escuro"), ("light", "Claro")], st["theme"], lambda v: st.update(theme=v)))
        row(1, "Tela cheia", lambda p: Seg(p, yn, st["fullscreen"], lambda v: st.update(fullscreen=v), minw=90))
        row(2, "Escala da interface", lambda p: Stepper(p, st["scale"], 70, 160, 10, lambda v: "%d%%" % v,
                                                         lambda v: st.update(scale=v)))
        row(3, "Fonte das notas", lambda p: Stepper(p, st["font_size"], 12, 32, 2, lambda v: "%d px" % v,
                                                     lambda v: st.update(font_size=v)), "Tamanho do texto no editor")

        def backup_row(p):
            f = tk.Frame(p, bg=C["card"])
            Btn(f, "Exportar", self.export_all, icon="backup").pack(side="left", padx=(0, sz(8)))
            Btn(f, "Importar", self.import_all, icon="restore").pack(side="left")
            return f
        row(4, "Backup completo", backup_row, "Todos os jogos e notas")

        def close():
            m.close()
            save_config(self.cfg)
            self.apply_look()
            self.build_ui()
        m.buttons([("Fechar", close, "primary")])

    def run(self):
        self.root.mainloop()


def main():
    App().run()


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# =============================================================================
#  PROJETO: µSave Backup
#  VERSÃO: 0.1.0
#  DATA: Outubro / 2026
#  DESCRIÇÃO: Gerenciador de backups de saves de jogos, com perfis por jogo.
#             Pensado para o Steam Deck (1280x800, tela cheia, touch screen).
#  TECNOLOGIAS: Python 3.9+, Tkinter (biblioteca padrão, sem dependências).
#
#  COMPRESSÃO / MULTI-NÚCLEO
#   • .tar.gz  -> o fluxo tar é dividido em blocos de 2 MiB; cada bloco vira um
#                 "membro gzip" independente, comprimido em paralelo (estilo
#                 pigz). Os membros levam o tamanho comprimido num campo extra
#                 do cabeçalho, o que permite DESCOMPRIMIR em paralelo também.
#                 O resultado continua sendo um .tar.gz padrão (tar xzf funciona).
#   • .zip     -> cada arquivo (em lotes) é comprimido em um processo diferente
#                 e montado em ordem; a restauração também é paralela.
#   • Toda a operação roda numa thread; a interface nunca trava.
# =============================================================================
import collections
import concurrent.futures as cf
import fnmatch
import io
import json
import math
import multiprocessing as mp
import os
import queue
import re
import shutil
import struct
import sys
import tarfile
import tempfile
import threading
import time
import uuid
import zipfile
import zlib
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import font as tkfont

APP_NAME = "µSave Backup"
APP_VER = "0.1.0"
CFG_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "usave-backup"
CFG_FILE = CFG_DIR / "config.json"

CHUNK = 2 * 1024 * 1024          # bloco do tar.gz paralelo
BIG_FILE = 16 * 1024 * 1024      # acima disso o zip usa arquivo temporário


# =============================================================================
#  MOTOR DE BACKUP (sem dependência de interface)
# =============================================================================
class Cancelled(Exception):
    pass


class NoFiles(RuntimeError):
    pass


class Progress:
    """Estado compartilhado entre a thread de trabalho e a interface."""

    def __init__(self):
        self.warnings = []
        self.reset("")

    def reset(self, text=""):
        self.text = text
        self.total = 0
        self.done = 0
        self.files = 0
        self.files_total = 0
        self.t0 = time.time()


def safe_name(name):
    s = re.sub(r"[^\w\-]+", "_", name, flags=re.UNICODE)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "perfil"


def parse_excludes(text):
    return [p.strip() for p in re.split(r"[,;\n]", text or "") if p.strip()]


def backup_dir(profile, settings):
    if profile.get("dest"):
        return Path(profile["dest"])
    return Path(settings["default_dir"]) / safe_name(profile["name"])


BK_RE = re.compile(r"^(?P<p>.+?)__(?P<ts>\d{8}-\d{6})(?P<pre>__pre)?(?: - (?P<c>.*))?\.(?P<ext>zip|tar\.gz)$")
MAX_COMMENT = 32


def clean_comment(text):
    """Remove caracteres inválidos em nomes de arquivo e limita a 32 caracteres."""
    t = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", text or "")
    return re.sub(r"\s+", " ", t).strip()[:MAX_COMMENT].strip()


def set_comment(b, comment):
    """Renomeia o backup: <base> - <comentário>.<ext>. Retorna o novo caminho."""
    c = clean_comment(comment)
    new = os.path.join(os.path.dirname(b["path"]), b["base"] + (" - " + c if c else "") + "." + b["fmt"])
    if new != b["path"]:
        if os.path.exists(new):
            raise FileExistsError("Já existe um arquivo com esse nome.")
        os.rename(b["path"], new)
    return new


def list_backups(profile, settings):
    d = backup_dir(profile, settings)
    out = []
    try:
        names = os.listdir(d)
    except OSError:
        return out
    sp = safe_name(profile["name"])
    for n in names:
        m = BK_RE.match(n)
        if not m or m.group("p") != sp:
            continue
        try:
            st = os.stat(d / n)
            ts = datetime.strptime(m.group("ts"), "%Y%m%d-%H%M%S")
        except (OSError, ValueError):
            continue
        out.append(dict(path=str(d / n), name=n, ts=ts, size=st.st_size,
                        fmt=m.group("ext"), pre=bool(m.group("pre")), comment=m.group("c") or "",
                        base="%s__%s%s" % (m.group("p"), m.group("ts"), m.group("pre") or "")))
    out.sort(key=lambda b: b["ts"], reverse=True)
    return out


def apply_retention(profile, settings):
    bks = list_backups(profile, settings)
    keep = int(profile.get("keep", 0) or 0)
    regular = [b for b in bks if not b["pre"]]
    pre = [b for b in bks if b["pre"]]
    doomed = pre[3:]
    if keep > 0:
        doomed += regular[keep:]
    for b in doomed:
        try:
            os.remove(b["path"])
        except OSError:
            pass


def _excluded(name, rel, pats):
    return any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel, p) for p in pats)


def scan_sources(sources, excludes, prog=None):
    """Lista (caminho, nome_no_arquivo, tamanho). Cada pasta vira 'srcN/'."""
    out = []
    for i, src in enumerate(sources):
        base = os.path.abspath(os.path.expanduser(src))
        if not os.path.isdir(base):
            if prog:
                prog.warnings.append("Pasta não encontrada: " + base)
            continue
        for root, dirs, names in os.walk(base):
            rel_root = os.path.relpath(root, base).replace(os.sep, "/")
            rel_root = "" if rel_root == "." else rel_root + "/"
            dirs[:] = sorted(d for d in dirs
                             if not _excluded(d, rel_root + d, excludes)
                             and not os.path.islink(os.path.join(root, d)))
            for n in sorted(names):
                rel = rel_root + n
                if _excluded(n, rel, excludes):
                    continue
                p = os.path.join(root, n)
                try:
                    if os.path.islink(p) or not os.path.isfile(p):
                        continue
                    out.append((p, "src%d/%s" % (i, rel), os.path.getsize(p)))
                except OSError:
                    continue
    return out


def make_batches(items, target=4 * 1024 * 1024, maxn=32):
    """Agrupa itens (..., tamanho) em lotes para reduzir o custo de IPC."""
    batch, size = [], 0
    for it in items:
        batch.append(it)
        size += it[-1]
        if size >= target or len(batch) >= maxn:
            yield batch
            batch, size = [], 0
    if batch:
        yield batch


def _worker_init():
    try:
        os.nice(10)   # processos de compressão não competem com a interface
    except Exception:
        pass


def make_pool(workers):
    return cf.ProcessPoolExecutor(max_workers=max(1, workers),
                                  mp_context=mp.get_context("spawn"),
                                  initializer=_worker_init)


# ---------------------------------------------------------------- ZIP ------
def _deflate_batch(batch, level, tmpdir):
    """Roda em processo separado. Retorna dados deflate de cada arquivo."""
    out = []
    for path, arc, _size in batch:
        try:
            st = os.stat(path)
            co = zlib.compressobj(level, zlib.DEFLATED, -15)
            crc = usize = csize = 0
            parts, tmpf, tmpname = [], None, None
            with open(path, "rb") as f:
                while True:
                    b = f.read(1 << 20)
                    if not b:
                        break
                    crc = zlib.crc32(b, crc)
                    usize += len(b)
                    c = co.compress(b)
                    if not c:
                        continue
                    csize += len(c)
                    if tmpf is None and csize > BIG_FILE:
                        fd, tmpname = tempfile.mkstemp(dir=tmpdir)
                        tmpf = os.fdopen(fd, "wb")
                        for pc in parts:
                            tmpf.write(pc)
                        parts = []
                    if tmpf:
                        tmpf.write(c)
                    else:
                        parts.append(c)
            tail = co.flush()
            csize += len(tail)
            if tmpf:
                tmpf.write(tail)
                tmpf.close()
                data = None
            else:
                parts.append(tail)
                data = b"".join(parts)
            out.append((arc, data, tmpname, crc & 0xFFFFFFFF, usize, csize,
                        st.st_mtime, st.st_mode, None))
        except OSError as e:
            out.append((arc, None, None, 0, 0, 0, 0, 0, str(e)))
    return out


class ZipOut:
    """Escritor de ZIP mínimo que aceita dados deflate já comprimidos."""
    LIM = 0xFFFFFFFF

    def __init__(self, f):
        self.f = f
        self.cd = []

    @staticmethod
    def _dos(ts):
        lt = time.localtime(ts)
        y = max(lt.tm_year, 1980)
        return ((lt.tm_hour << 11) | (lt.tm_min << 5) | (lt.tm_sec // 2),
                ((y - 1980) << 9) | (lt.tm_mon << 5) | lt.tm_mday)

    def add(self, arc, crc, usize, csize, ts, mode, chunks):
        off = self.f.tell()
        if max(usize, csize, off) >= self.LIM or len(self.cd) >= 0xFFFF:
            raise OverflowError("Backup grande demais para .zip (limite de 4 GB). Use o formato .tar.gz.")
        name = arc.encode("utf-8")
        t, d = self._dos(ts)
        self.f.write(struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, 0x0800, 8, t, d,
                                 crc, csize, usize, len(name), 0) + name)
        for c in chunks:
            self.f.write(c)
        self.cd.append((name, t, d, crc, csize, usize, mode, off))

    def add_bytes(self, arc, data, level):
        co = zlib.compressobj(level, zlib.DEFLATED, -15)
        comp = co.compress(data) + co.flush()
        self.add(arc, zlib.crc32(data) & 0xFFFFFFFF, len(data), len(comp),
                 time.time(), 0o100644, [comp])

    def finish(self):
        start = self.f.tell()
        for name, t, d, crc, cs, us, mode, off in self.cd:
            self.f.write(struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, (3 << 8) | 20, 20,
                                     0x0800, 8, t, d, crc, cs, us, len(name), 0, 0, 0, 0,
                                     (mode & 0xFFFF) << 16, off) + name)
        end = self.f.tell()
        n = len(self.cd)
        self.f.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, n, n, end - start, start, 0))


def _backup_zip(files, manifest, tmp_path, level, workers, prog, cancel):
    pool = make_pool(workers)
    pending = collections.deque()
    tmpdir = tempfile.mkdtemp(prefix=".usave-", dir=os.path.dirname(tmp_path))
    try:
        with open(tmp_path, "wb") as out:
            z = ZipOut(out)
            z.add_bytes("manifest.json", json.dumps(manifest, ensure_ascii=False).encode(), level)

            def drain_one():
                for arc, data, tname, crc, us, cs, mt, mode, err in pending.popleft().result():
                    if err:
                        prog.warnings.append("Ignorado: %s (%s)" % (arc, err))
                        continue
                    if data is not None:
                        z.add(arc, crc, us, cs, mt, mode, [data])
                    else:
                        def gen(p=tname):
                            with open(p, "rb") as tf:
                                while True:
                                    b = tf.read(1 << 20)
                                    if not b:
                                        break
                                    yield b
                        z.add(arc, crc, us, cs, mt, mode, gen())
                        os.remove(tname)
                    prog.done += us
                    prog.files += 1

            for batch in make_batches(files):
                if cancel.is_set():
                    raise Cancelled()
                pending.append(pool.submit(_deflate_batch, batch, level, tmpdir))
                while len(pending) >= workers * 2:
                    drain_one()
            while pending:
                if cancel.is_set():
                    raise Cancelled()
                drain_one()
            z.finish()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
        shutil.rmtree(tmpdir, ignore_errors=True)


def _extract_batch(zpath, batch):
    done = []
    with zipfile.ZipFile(zpath) as zf:
        for arc, dest, _size in batch:
            zi = zf.getinfo(arc)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".usave-tmp"
            with zf.open(zi) as s, open(tmp, "wb") as d:
                shutil.copyfileobj(s, d, 1 << 20)
            os.replace(tmp, dest)
            ts = time.mktime(tuple(zi.date_time) + (0, 0, -1))
            os.utime(dest, (ts, ts))
            done.append(zi.file_size)
    return done


def resolve_dest(manifest, arc):
    pre, _, rel = arc.partition("/")
    base = manifest.get("sources", {}).get(pre)
    if not base or not rel:
        return None
    parts = rel.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    return os.path.join(base, *parts)


def _restore_zip(path, workers, prog, cancel):
    with zipfile.ZipFile(path) as zf:
        manifest = json.loads(zf.read("manifest.json"))
        items = []
        for zi in zf.infolist():
            if zi.is_dir() or zi.filename == "manifest.json":
                continue
            dest = resolve_dest(manifest, zi.filename)
            if dest:
                items.append((zi.filename, dest, zi.file_size))
    prog.total = sum(i[2] for i in items)
    prog.files_total = len(items)
    pool = make_pool(workers)
    pending = collections.deque()

    def drain_one():
        sizes = pending.popleft().result()
        prog.done += sum(sizes)
        prog.files += len(sizes)

    try:
        for batch in make_batches(items):
            if cancel.is_set():
                raise Cancelled()
            pending.append(pool.submit(_extract_batch, path, batch))
            while len(pending) >= workers * 2:
                drain_one()
        while pending:
            if cancel.is_set():
                raise Cancelled()
            drain_one()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


# ------------------------------------------------------------- TAR.GZ ------
def _gz_member(data, level):
    """Membro gzip válido, com o tamanho comprimido em um campo extra 'PZ'."""
    co = zlib.compressobj(level, zlib.DEFLATED, -15)
    comp = co.compress(data) + co.flush()
    hdr = (b"\x1f\x8b\x08\x04" + b"\0\0\0\0" + b"\x00\xff" + struct.pack("<H", 8)
           + b"PZ" + struct.pack("<H", 4) + struct.pack("<I", len(comp)))
    return hdr + comp + struct.pack("<II", zlib.crc32(data) & 0xFFFFFFFF, len(data) & 0xFFFFFFFF)


def _gunzip_member(blob):
    data = zlib.decompress(blob[20:-8], -15)
    crc, isize = struct.unpack("<II", blob[-8:])
    if (zlib.crc32(data) & 0xFFFFFFFF) != crc or (len(data) & 0xFFFFFFFF) != isize:
        raise ValueError("Bloco corrompido (CRC não confere).")
    return data


class ParallelGzipWriter:
    """Objeto de arquivo para o tarfile: comprime blocos em paralelo, em ordem."""

    def __init__(self, out, pool, level, workers, cancel):
        self.out, self.pool, self.level = out, pool, level
        self.window = workers * 2
        self.cancel = cancel
        self.buf = bytearray()
        self.pending = collections.deque()

    def write(self, b):
        self.buf += b
        while len(self.buf) >= CHUNK:
            self._submit(bytes(self.buf[:CHUNK]))
            del self.buf[:CHUNK]
        return len(b)

    def _submit(self, data):
        if self.cancel.is_set():
            raise Cancelled()
        self.pending.append(self.pool.submit(_gz_member, data, self.level))
        while len(self.pending) >= self.window:
            self.out.write(self.pending.popleft().result())

    def close(self):
        if self.buf:
            self._submit(bytes(self.buf))
            self.buf = bytearray()
        while self.pending:
            self.out.write(self.pending.popleft().result())


class _CountReader:
    def __init__(self, f, prog, cancel):
        self.f, self.prog, self.cancel = f, prog, cancel

    def read(self, n=-1):
        if self.cancel.is_set():
            raise Cancelled()
        b = self.f.read(n)
        self.prog.done += len(b)
        return b


def _backup_targz(files, manifest, tmp_path, level, workers, prog, cancel):
    pool = make_pool(workers)
    try:
        with open(tmp_path, "wb") as out:
            w = ParallelGzipWriter(out, pool, level, workers, cancel)
            tar = tarfile.open(fileobj=w, mode="w|", format=tarfile.PAX_FORMAT, encoding="utf-8")
            data = json.dumps(manifest, ensure_ascii=False).encode()
            ti = tarfile.TarInfo("manifest.json")
            ti.size, ti.mtime = len(data), time.time()
            tar.addfile(ti, io.BytesIO(data))
            for path, arc, _size in files:
                if cancel.is_set():
                    raise Cancelled()
                try:
                    f = open(path, "rb")
                except OSError as e:
                    prog.warnings.append("Ignorado: %s (%s)" % (arc, e))
                    continue
                with f:
                    ti = tar.gettarinfo(arcname=arc, fileobj=f)
                    ti.uid = ti.gid = 0
                    ti.uname = ti.gname = ""
                    tar.addfile(ti, _CountReader(f, prog, cancel))
                prog.files += 1
            tar.close()
            w.close()
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def scan_members(path):
    """Retorna [(offset, tamanho)] dos membros gzip paralelos, ou None."""
    size = os.path.getsize(path)
    members, off = [], 0
    with open(path, "rb") as f:
        while off < size:
            f.seek(off)
            h = f.read(20)
            if (len(h) < 20 or h[:4] != b"\x1f\x8b\x08\x04" or h[10:12] != b"\x08\x00"
                    or h[12:14] != b"PZ"):
                return None
            total = 28 + struct.unpack("<I", h[16:20])[0]
            members.append((off, total))
            off += total
    return members if off == size else None


class ParallelGunzip:
    """Lê o tar.gz descomprimindo vários membros ao mesmo tempo."""

    def __init__(self, path, members, pool, window, prog, cancel):
        self.f = open(path, "rb")
        self.members, self.pool, self.window = members, pool, window
        self.prog, self.cancel = prog, cancel
        self.idx = 0
        self.pending = collections.deque()
        self.buf = bytearray()

    def _fill(self):
        while self.idx < len(self.members) and len(self.pending) < self.window:
            off, total = self.members[self.idx]
            self.idx += 1
            self.f.seek(off)
            self.pending.append(self.pool.submit(_gunzip_member, self.f.read(total)))
            self.prog.done += total

    def read(self, n=-1):
        while n < 0 or len(self.buf) < n:
            if self.cancel.is_set():
                raise Cancelled()
            self._fill()
            if not self.pending:
                break
            self.buf += self.pending.popleft().result()
        if n < 0:
            n = len(self.buf)
        out = bytes(self.buf[:n])
        del self.buf[:n]
        return out

    def close(self):
        self.f.close()


class _PosReader:
    def __init__(self, f, prog, cancel):
        self.f, self.prog, self.cancel = f, prog, cancel

    def read(self, n=-1):
        if self.cancel.is_set():
            raise Cancelled()
        b = self.f.read(n)
        self.prog.done = self.f.tell()
        return b


def _write_stream(src, dest, mtime):
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    tmp = dest + ".usave-tmp"
    with open(tmp, "wb") as d:
        shutil.copyfileobj(src, d, 1 << 20)
    os.replace(tmp, dest)
    os.utime(dest, (mtime, mtime))


def _restore_targz(path, workers, prog, cancel):
    prog.total = os.path.getsize(path)
    members = scan_members(path)
    pool, src, rawf = None, None, None
    try:
        if members:
            pool = make_pool(workers)
            src = ParallelGunzip(path, members, pool, workers * 2, prog, cancel)
            tar = tarfile.open(fileobj=src, mode="r|")
        else:   # tar.gz "comum", criado por outra ferramenta: leitura sequencial
            rawf = open(path, "rb")
            tar = tarfile.open(fileobj=_PosReader(rawf, prog, cancel), mode="r|gz")
        manifest = None
        with tar:
            for ti in tar:
                if cancel.is_set():
                    raise Cancelled()
                if ti.name == "manifest.json":
                    manifest = json.loads(tar.extractfile(ti).read())
                    prog.files_total = manifest.get("files", 0)
                    continue
                if manifest is None:
                    raise ValueError("Backup sem manifesto: não é um backup do µSave.")
                if not ti.isfile():
                    continue
                dest = resolve_dest(manifest, ti.name)
                if dest:
                    _write_stream(tar.extractfile(ti), dest, ti.mtime)
                    prog.files += 1
    finally:
        if src:
            src.close()
        if rawf:
            rawf.close()
        if pool:
            pool.shutdown(wait=True, cancel_futures=True)


# --------------------------------------------------------- OPERAÇÕES -------
def run_backup(profile, settings, prog, cancel, pre=False):
    prog.reset("Analisando arquivos…")
    excludes = parse_excludes(profile.get("exclude"))
    files = scan_sources(profile["sources"], excludes, prog)
    if not files:
        raise NoFiles("Nenhum arquivo encontrado nas pastas do perfil.")
    prog.total = sum(f[2] for f in files)
    prog.files_total = len(files)
    prog.text = "Backup de segurança…" if pre else "Compactando…"
    prog.t0 = time.time()
    fmt = profile.get("format", "zip")
    d = backup_dir(profile, settings)
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    final = d / ("%s__%s%s.%s" % (safe_name(profile["name"]), stamp, "__pre" if pre else "", fmt))
    tmp = str(final) + ".part"
    manifest = dict(app=APP_NAME, version=1, profile=profile["name"],
                    created=datetime.now().isoformat(timespec="seconds"),
                    files=len(files), bytes=prog.total,
                    sources={"src%d" % i: os.path.abspath(os.path.expanduser(s))
                             for i, s in enumerate(profile["sources"])})
    level = int(settings.get("level", 6))
    workers = int(settings.get("workers", 2))
    try:
        (_backup_zip if fmt == "zip" else _backup_targz)(files, manifest, tmp, level, workers, prog, cancel)
        os.replace(tmp, final)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    apply_retention(profile, settings)
    return dict(path=str(final), files=prog.files, bytes=prog.total,
                csize=os.path.getsize(final), elapsed=time.time() - prog.t0)


def run_restore(profile, settings, bpath, prog, cancel):
    if settings.get("safety_backup", True):
        try:
            run_backup(profile, settings, prog, cancel, pre=True)
        except NoFiles:
            pass
    prog.reset("Restaurando…")
    workers = int(settings.get("workers", 2))
    if bpath.endswith(".zip"):
        _restore_zip(bpath, workers, prog, cancel)
    else:
        _restore_targz(bpath, workers, prog, cancel)
    return dict(path=bpath, files=prog.files, bytes=prog.done, elapsed=time.time() - prog.t0)


# =============================================================================
#  CONFIGURAÇÃO
# =============================================================================
def default_settings():
    cpu = os.cpu_count() or 2
    return dict(theme="dark", fullscreen=True, workers=max(1, cpu - 1), level=6,
                default_format="zip", safety_backup=True, scale=100,
                default_dir=str(Path.home() / "GameSaveBackups"))


def load_config():
    cfg = dict(settings=default_settings(), profiles=[])
    try:
        data = json.loads(CFG_FILE.read_text(encoding="utf-8"))
        cfg["settings"].update(data.get("settings", {}))
        cfg["profiles"] = data.get("profiles", [])
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg):
    try:
        CFG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CFG_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CFG_FILE)
    except OSError as e:
        print("Falha ao salvar configuração:", e, file=sys.stderr)


# =============================================================================
#  TEMA (Ubuntu escuro / claro — mesmas cores do µNFe Editor)
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


def fmt_size(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024 or u == "GB":
            return ("%d %s" % (n, u)) if u == "B" else ("%.1f %s" % (n, u))
        n /= 1024.0


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
        self.value = min(self.hi, max(self.lo, self.value + d * self.step))
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
        self.bar = tk.Canvas(self, width=sz(8), bg=bg, highlightthickness=0, bd=0)
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
    """Seletor de pastas próprio (o diálogo nativo do Tk é ruim para toque)."""

    def __init__(self, app, start, on_pick, title="Escolher pasta"):
        self.app, self.on_pick = app, on_pick
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
        compat = Path.home() / ".local/share/Steam/steamapps/compatdata"
        if compat.is_dir():
            Btn(bar, "Saves Proton", lambda: self.go(compat), icon="folder").pack(side="left", padx=(0, sz(6)))
        self.hbtn = Btn(bar, "Ocultas: não", self.toggle_hidden)
        self.hbtn.pack(side="right")
        self.sf = ScrollFrame(self.m.body, bg=C["bg"])
        self.sf.pack(fill="both", expand=True)
        self.m.buttons([("Cancelar", self.m.close, "ghost"),
                        ("Selecionar esta pasta", self.pick, "primary")])
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
        for n in dirs[:300]:
            self.row(n, "folder", lambda n=n: self.go(self.path / n))
        for n in files[:60]:
            self.row(n, "file", None, dim=True)
        if len(dirs) > 300:
            L(self.sf.inner, "… mais de 300 pastas; navegue por um nível acima.", 13, "mut").pack(pady=sz(8))
        if not dirs and not files:
            L(self.sf.inner, "Pasta vazia.", 15, "mut").pack(pady=sz(20))


class ProgressDialog:
    def __init__(self, app, title, job, on_done):
        self.app, self.on_done = app, on_done
        self.prog, self.cancel, self.q = Progress(), threading.Event(), queue.Queue()
        self.finished = False
        m = self.m = Modal(app, title, width=760, closable=False)
        self.lbl = L(m.body, "Preparando…", 18, "ink", bold=True, bg=C["card"], anchor="w")
        self.lbl.pack(fill="x", pady=(sz(8), sz(12)))
        self.bar = tk.Canvas(m.body, height=sz(30), bg=C["card"], highlightthickness=0)
        self.bar.pack(fill="x")
        self.stats = L(m.body, "", 14, "mut", bg=C["card"], anchor="w", justify="left", wraplength=sz(700))
        self.stats.pack(fill="x", pady=(sz(12), sz(4)))
        self.msg = L(m.body, "", 14, "ink", bg=C["card"], anchor="w", justify="left", wraplength=sz(700))
        self.msg.pack(fill="x", pady=(0, sz(8)))
        self.cbtn = Btn(m.foot, "Cancelar", self._cancel, "ghost", minw=sz(170))
        self.cbtn.pack(side="right")
        threading.Thread(target=self._run, args=(job,), daemon=True).start()
        self._poll()

    def _run(self, job):
        try:
            self.q.put(("ok", job(self.prog, self.cancel)))
        except Cancelled:
            self.q.put(("cancel", None))
        except BaseException as e:
            import traceback
            traceback.print_exc()
            self.q.put(("err", e))

    def _cancel(self):
        self.cancel.set()
        self.cbtn.set_disabled(True)
        self.lbl.config(text="Cancelando…")

    def _draw_bar(self, frac):
        self.bar.delete("all")
        w, h = self.bar.winfo_width(), self.bar.winfo_height()
        rrect(self.bar, 1, 1, w - 1, h - 1, sz(8), fill=C["bg"], outline=C["line"])
        if frac > 0:
            rrect(self.bar, 1, 1, max(sz(18), int(w * frac)), h - 1, sz(8), fill=C["acc"], outline=C["acc"])

    def _poll(self):
        if self.finished or self.m not in Modal.stack:
            return
        p = self.prog
        if not self.cancel.is_set():
            self.lbl.config(text=p.text)
        frac = (p.done / p.total) if p.total else 0
        self._draw_bar(min(1.0, frac))
        el = max(0.001, time.time() - p.t0)
        ft = ("/%d" % p.files_total) if p.files_total else ""
        self.stats.config(text="%d%s arquivos  •  %s / %s  •  %s/s  •  %d%%" % (
            p.files, ft, fmt_size(p.done), fmt_size(p.total), fmt_size(p.done / el), int(frac * 100)))
        try:
            kind, res = self.q.get_nowait()
        except queue.Empty:
            self.m.ov.after(120, self._poll)
            return
        self._finish(kind, res)

    def _finish(self, kind, res):
        self.finished = True
        self.m.closable = True
        self.cbtn.destroy()
        warn = ("\n⚠ %d aviso(s): %s" % (len(self.prog.warnings), self.prog.warnings[0])) if self.prog.warnings else ""
        if kind == "ok":
            self._draw_bar(1.0)
            self.lbl.config(text="Concluído", fg=C["ok"])
            self.msg.config(text="%d arquivos em %.1f s%s" % (res.get("files", 0), res.get("elapsed", 0), warn))
        elif kind == "cancel":
            self.lbl.config(text="Cancelado", fg=C["mut"])
            self.msg.config(text="Nenhum arquivo parcial foi mantido." if "csize" not in (res or {}) else "")
        else:
            self.lbl.config(text="Erro", fg=C["danger"])
            self.msg.config(text=str(res))
        Btn(self.m.foot, "Fechar", self.m.close, "primary", minw=sz(170)).pack(side="right")
        self.on_done(kind, res)


# =============================================================================
#  EDITOR DE PERFIL
# =============================================================================
class ProfileEditor:
    def __init__(self, app, profile=None):
        self.app, self.orig = app, profile
        st = app.cfg["settings"]
        p = dict(profile) if profile else dict(id=uuid.uuid4().hex[:8], name="", sources=[], dest="",
                                               format=st["default_format"], keep=10, exclude="*.log, *.tmp")
        p["sources"] = list(p["sources"])
        self.p = p
        m = self.m = Modal(app, "Editar perfil" if profile else "Novo perfil", width=1120)
        card = C["card"]
        cols = tk.Frame(m.body, bg=card)
        cols.pack(fill="x")
        left = tk.Frame(cols, bg=card, width=sz(480))
        left.pack(side="left", fill="y", padx=(0, sz(24)))
        right = tk.Frame(cols, bg=card)
        right.pack(side="left", fill="both", expand=True)

        L(left, "Jogo / nome do perfil", 13, "mut", anchor="w").pack(fill="x")
        self.nv = tk.StringVar(value=p["name"])
        mk_entry(left, self.nv).pack(fill="x", ipady=sz(1), pady=(sz(2), sz(10)))
        L(left, "Formato", 13, "mut", anchor="w").pack(fill="x")
        self.seg = Seg(left, [("zip", "ZIP"), ("tar.gz", "TAR.GZ")], p["format"], self._fmt)
        self.seg.pack(anchor="w", pady=(sz(2), sz(10)))
        L(left, "Manter os últimos backups", 13, "mut", anchor="w").pack(fill="x")
        Stepper(left, p["keep"], 0, 99, 1, lambda v: "Todos" if v == 0 else str(v),
                lambda v: p.update(keep=v)).pack(anchor="w", pady=(sz(2), sz(10)))
        L(left, "Ignorar arquivos (padrões separados por vírgula)", 13, "mut", anchor="w").pack(fill="x")
        self.ev = tk.StringVar(value=p["exclude"])
        mk_entry(left, self.ev).pack(fill="x", ipady=sz(1), pady=(sz(2), 0))

        L(right, "Pastas de saves", 13, "mut", anchor="w").pack(fill="x")
        self.sf = ScrollFrame(right, bg=C["bg"], height=sz(236))
        self.sf.pack(fill="x", pady=(sz(2), sz(8)))
        Btn(right, "Adicionar pasta…", self.add_src, "primary", icon="plus").pack(anchor="w")

        L(m.body, "Destino dos backups", 13, "mut", anchor="w").pack(fill="x", pady=(sz(12), 0))
        row = tk.Frame(m.body, bg=card)
        row.pack(fill="x", pady=(sz(2), 0))
        Btn(row, "Padrão", self.reset_dest).pack(side="right", padx=(sz(6), 0))
        Btn(row, "Escolher…", self.pick_dest, icon="folder").pack(side="right")
        self.dl = L(row, "", 14, "ink", mono=True, anchor="w", wraplength=sz(700), justify="left")
        self.dl.pack(side="left", fill="x", expand=True)
        self.nv.trace_add("write", lambda *a: self.show_dest())
        self.err = L(m.body, "Dica: o teclado virtual abre com Steam + X.", 13, "mut", anchor="w")
        self.err.pack(fill="x", pady=(sz(10), 0))
        m.buttons([("Cancelar", m.close, "ghost"), ("Salvar", self.save, "primary")])
        self.show_src()
        self.show_dest()

    def _fmt(self, v):
        self.p["format"] = v

    def show_src(self):
        self.sf.clear()
        if not self.p["sources"]:
            L(self.sf.inner, "Nenhuma pasta. Toque em “Adicionar pasta…”.", 14, "mut").pack(pady=sz(20))
        for i, s in enumerate(self.p["sources"]):
            f = tk.Frame(self.sf.inner, bg=C["card"], highlightthickness=1, highlightbackground=C["line"])
            f.pack(fill="x", pady=(0, sz(4)), padx=(0, sz(4)))
            Btn(f, "", lambda i=i: self.del_src(i), "ghost", icon="close", w=sz(48), h=sz(48)).pack(
                side="right", padx=sz(4), pady=sz(4))
            tk.Label(f, text=s, font=F(14, mono=True), fg=C["ink"], bg=C["card"], anchor="w",
                     justify="left", wraplength=sz(420)).pack(side="left", fill="x", expand=True,
                                                              padx=sz(8), pady=sz(6))

    def show_dest(self):
        d = self.p["dest"] or "(padrão) %s" % (Path(self.app.cfg["settings"]["default_dir"]) / safe_name(self.nv.get() or "perfil"))
        self.dl.config(text=d)

    def add_src(self):
        start = self.p["sources"][-1] if self.p["sources"] else str(Path.home())

        def got(path):
            if path not in self.p["sources"]:
                self.p["sources"].append(path)
            self.show_src()
        FolderPicker(self.app, start, got, "Pasta de saves")

    def del_src(self, i):
        del self.p["sources"][i]
        self.show_src()

    def pick_dest(self):
        def got(path):
            self.p["dest"] = path
            self.show_dest()
        FolderPicker(self.app, self.p["dest"] or self.app.cfg["settings"]["default_dir"], got, "Destino dos backups")

    def reset_dest(self):
        self.p["dest"] = ""
        self.show_dest()

    def save(self):
        name = self.nv.get().strip()
        err = None
        if not name:
            err = "Informe um nome para o perfil."
        elif not self.p["sources"]:
            err = "Adicione pelo menos uma pasta de saves."
        else:
            for o in self.app.cfg["profiles"]:
                if o["id"] != self.p["id"] and safe_name(o["name"]) == safe_name(name):
                    err = "Já existe um perfil com esse nome."
        if err:
            self.err.config(text="⚠ " + err, fg=C["danger"])
            return
        self.p["name"] = name
        self.p["exclude"] = self.ev.get().strip()
        self.m.close()
        self.app.save_profile(self.p, self.orig)


# =============================================================================
#  APLICATIVO
# =============================================================================
class App:
    def __init__(self):
        global TOUCH, S
        self.cfg = load_config()
        self.root = tk.Tk()
        self.root.title("%s v%s" % (APP_NAME, APP_VER))
        self.root.geometry("1280x800")
        fams = set(tkfont.families())
        FONT["ui"] = next((f for f in ("Ubuntu", "Cantarell", "Noto Sans", "DejaVu Sans") if f in fams), "sans-serif")
        FONT["mono"] = next((f for f in ("Ubuntu Mono", "DejaVu Sans Mono", "Noto Sans Mono") if f in fams), "monospace")
        TOUCH = Touch(self.root)
        self.sel = self.cfg["profiles"][0]["id"] if self.cfg["profiles"] else None
        self.main = None
        self.apply_look()
        self.build_ui()
        self.root.bind("<Escape>", lambda e: Modal.stack and Modal.stack[-1].closable and Modal.stack[-1].close())
        self.root.protocol("WM_DELETE_WINDOW", self.quit_app)
        self.root.bind("<F11>", lambda e: self.toggle_fullscreen())
        self.root.bind("<Control-n>", lambda e: self.new_profile())

    # ---- aparência ---------------------------------------------------------
    def apply_look(self):
        global S
        st = self.cfg["settings"]
        C.clear()
        C.update(THEMES.get(st["theme"], THEMES["dark"]))
        S = max(0.7, min(1.6, st.get("scale", 100) / 100.0))
        self.root.configure(bg=C["bg"])
        self.root.attributes("-fullscreen", bool(st["fullscreen"]))

    def quit_app(self):
        if any(not m.closable for m in Modal.stack):
            self.set_status("Aguarde o fim (ou cancele) a operação em andamento.", "danger")
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
        self.main = tk.Frame(self.root, bg=C["bg"])
        self.main.pack(fill="both", expand=True)
        hd = tk.Frame(self.main, bg=C["bar"])
        hd.pack(fill="x")
        tk.Frame(self.main, bg=C["acc"], height=3).pack(fill="x")
        pad = tk.Frame(hd, bg=C["bar"])
        pad.pack(fill="x", padx=sz(14), pady=sz(10))
        Btn(pad, "Novo perfil", self.new_profile, "hdr", icon="plus").pack(side="left", padx=(0, sz(10)))
        tk.Frame(pad, bg="#6a4560", width=1, height=sz(34)).pack(side="left", padx=sz(6))
        Btn(pad, "", self.open_settings, "hdr", icon="gear", w=sz(56)).pack(side="left", padx=(sz(6), sz(8)))
        Btn(pad, "", self.open_help, "hdr", icon="help", w=sz(56)).pack(side="left")
        Btn(pad, "", self.quit_app, "hdr", icon="close", w=sz(56)).pack(side="right", padx=(sz(12), 0))
        info = tk.Frame(pad, bg=C["bar"])
        info.pack(side="right")
        tk.Label(info, text=APP_NAME, font=F(16, True, True), fg="#fff", bg=C["bar"], anchor="e").pack(anchor="e")
        self.hdr_sub = tk.Label(info, text="", font=F(12), fg="#cfc3cb", bg=C["bar"], anchor="e")
        self.hdr_sub.pack(anchor="e")

        self.foot = tk.Frame(self.main, bg=C["head"], highlightthickness=1, highlightbackground=C["line"])
        self.foot.pack(side="bottom", fill="x")
        self.status = tk.Label(self.foot, text="Pronto.", font=F(13), fg=C["mut"], bg=C["head"], anchor="w")
        self.status.pack(fill="x", padx=sz(14), pady=sz(5))

        body = tk.Frame(self.main, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=sz(14), pady=sz(12))
        left = tk.Frame(body, bg=C["bg"], width=sz(390))
        left.pack(side="left", fill="y", padx=(0, sz(14)))
        left.pack_propagate(False)
        L(left, "PERFIS", 13, "acc", bold=True, anchor="w").pack(fill="x", pady=(0, sz(6)))
        self.plist = ScrollFrame(left)
        self.plist.pack(fill="both", expand=True)
        self.detail = tk.Frame(body, bg=C["bg"])
        self.detail.pack(side="left", fill="both", expand=True)
        self.refresh()

    def set_status(self, text, color="mut"):
        self.status.config(text=text, fg=C[color])

    # ---- dados -------------------------------------------------------------
    def cur(self):
        return next((p for p in self.cfg["profiles"] if p["id"] == self.sel), None)

    def select(self, pid):
        self.sel = pid
        self.refresh()

    def refresh(self):
        self.fill_profiles()
        self.fill_detail()
        st = self.cfg["settings"]
        self.hdr_sub.config(text="v%s  •  %d perfis  •  %d núcleos" % (APP_VER, len(self.cfg["profiles"]), st["workers"]))

    def fill_profiles(self):
        sf = self.plist
        sf.clear()
        if not self.cfg["profiles"]:
            L(sf.inner, "Nenhum perfil ainda.\nToque em “Novo perfil”.", 15, "mut", justify="center").pack(pady=sz(60))
            return
        for p in self.cfg["profiles"]:
            sel = p["id"] == self.sel
            bg = C["sel"] if sel else C["card"]
            f = tk.Frame(sf.inner, bg=bg, highlightthickness=sz(2) if sel else 1,
                         highlightbackground=C["acc"] if sel else C["line"])
            f.pack(fill="x", pady=(0, sz(8)), padx=(0, sz(6)))
            bks = list_backups(p, self.cfg["settings"])
            reg = [b for b in bks if not b["pre"]]
            tk.Label(f, text=p["name"], font=F(19, True), fg=C["ink"], bg=bg, anchor="w",
                     wraplength=sz(340), justify="left").pack(fill="x", padx=sz(14), pady=(sz(10), 0))
            n = len(p["sources"])
            tk.Label(f, text="%d pasta%s  •  %s" % (n, "" if n == 1 else "s", p["format"].upper()),
                     font=F(13), fg=C["mut"], bg=bg, anchor="w").pack(fill="x", padx=sz(14))
            last = ("Último: " + fmt_dt(reg[0]["ts"])) if reg else "Sem backups"
            tk.Label(f, text="%s  (%d)" % (last, len(reg)), font=F(13), fg=C["acc"] if reg else C["mut"],
                     bg=bg, anchor="w").pack(fill="x", padx=sz(14), pady=(0, sz(10)))
            bind_tap(f, lambda pid=p["id"]: self.select(pid))

    def fill_detail(self):
        d = self.detail
        for c in d.winfo_children():
            c.destroy()
        p = self.cur()
        if not p:
            L(d, "Crie um perfil para começar.\n\nCada jogo tem suas pastas de saves;\no perfil guarda quais são e para onde vão os backups.",
              16, "mut", justify="center").pack(expand=True)
            return
        st = self.cfg["settings"]
        top = tk.Frame(d, bg=C["bg"])
        top.pack(fill="x")
        Btn(top, "", self.delete_profile, "danger", icon="trash", w=sz(56)).pack(side="right", padx=(sz(8), 0))
        Btn(top, "Editar", self.edit_profile, icon="edit").pack(side="right")
        L(top, p["name"], 28, "acc", bold=True, anchor="w").pack(side="left", fill="x", expand=True)

        info = tk.Frame(d, bg=C["card"], highlightthickness=1, highlightbackground=C["line"])
        info.pack(fill="x", pady=(sz(10), sz(12)))
        rows = [("Saves", "\n".join(p["sources"]) or "—"),
                ("Destino", str(backup_dir(p, st))),
                ("Formato", "%s  •  manter %s%s" % (p["format"].upper(), p["keep"] or "todos",
                                                   "  •  ignorar: " + p["exclude"] if p["exclude"] else ""))]
        for k, v in rows:
            r = tk.Frame(info, bg=C["card"])
            r.pack(fill="x", padx=sz(14), pady=sz(3))
            L(r, k, 13, "mut", bg=C["card"], width=8, anchor="nw").pack(side="left", anchor="n")
            tk.Label(r, text=v, font=F(13, mono=True), fg=C["ink"], bg=C["card"], anchor="w",
                     justify="left", wraplength=sz(700)).pack(side="left", fill="x", expand=True)
        Btn(d, "Fazer backup agora", self.do_backup, "primary", icon="backup", h=sz(64), size=20).pack(fill="x")

        bks = list_backups(p, st)
        L(d, "BACKUPS SALVOS (%d)" % len(bks), 13, "acc", bold=True, anchor="w").pack(fill="x", pady=(sz(14), sz(6)))
        sf = ScrollFrame(d)
        sf.pack(fill="both", expand=True)
        if not bks:
            L(sf.inner, "Nenhum backup ainda.", 15, "mut").pack(pady=sz(30))
        for b in bks:
            r = tk.Frame(sf.inner, bg=C["card"], highlightthickness=1, highlightbackground=C["line"])
            r.pack(fill="x", pady=(0, sz(6)), padx=(0, sz(6)))
            Btn(r, "", lambda b=b: self.delete_backup(b), "ghost", icon="trash", w=sz(56), h=sz(52)).pack(
                side="right", padx=(sz(6), sz(8)), pady=sz(8))
            Btn(r, "", lambda b=b: self.edit_comment(b), "ghost", icon="edit", w=sz(56), h=sz(52)).pack(
                side="right", padx=(sz(6), 0), pady=sz(8))
            Btn(r, "Restaurar", lambda b=b: self.do_restore(b), "ghost", icon="restore", h=sz(52)).pack(
                side="right", pady=sz(8))
            col = tk.Frame(r, bg=C["card"])
            col.pack(side="left", fill="x", expand=True, padx=sz(14), pady=sz(8))
            line = tk.Frame(col, bg=C["card"])
            line.pack(fill="x")
            tk.Label(line, text=fmt_dt(b["ts"]) + ("  ⟲ segurança" if b["pre"] else ""), font=F(17, True),
                     fg=C["mut"] if b["pre"] else C["ink"], bg=C["card"]).pack(side="left")
            if b["comment"]:
                tk.Label(line, text=b["comment"], font=F(16, True), fg=C["ink"],
                         bg=C["card"]).pack(side="left", padx=(sz(14), 0))
            tk.Label(col, text="%s  •  %s" % (fmt_size(b["size"]), b["fmt"].upper()), font=F(13),
                     fg=C["mut"], bg=C["card"], anchor="w").pack(fill="x")

    # ---- perfis ------------------------------------------------------------
    def new_profile(self):
        ProfileEditor(self)

    def edit_profile(self):
        if self.cur():
            ProfileEditor(self, self.cur())

    def save_profile(self, p, orig):
        st = self.cfg["settings"]
        if orig:
            od, nd = backup_dir(orig, st), backup_dir(p, st)
            if safe_name(orig["name"]) != safe_name(p["name"]) and od == nd:
                for b in list_backups(orig, st):   # acompanha o novo nome
                    new = b["name"].replace(safe_name(orig["name"]) + "__", safe_name(p["name"]) + "__", 1)
                    try:
                        os.rename(b["path"], os.path.join(os.path.dirname(b["path"]), new))
                    except OSError:
                        pass
            self.cfg["profiles"] = [p if x["id"] == p["id"] else x for x in self.cfg["profiles"]]
        else:
            self.cfg["profiles"].append(p)
        self.sel = p["id"]
        save_config(self.cfg)
        self.refresh()
        self.set_status("Perfil “%s” salvo." % p["name"], "ok")

    def delete_profile(self):
        p = self.cur()
        if not p:
            return

        def go():
            self.cfg["profiles"] = [x for x in self.cfg["profiles"] if x["id"] != p["id"]]
            self.sel = self.cfg["profiles"][0]["id"] if self.cfg["profiles"] else None
            save_config(self.cfg)
            self.refresh()
            self.set_status("Perfil removido. Os arquivos de backup foram mantidos no disco.")
        confirm(self, "Excluir perfil", "Excluir o perfil “%s”?\n\nOs backups já salvos NÃO são apagados do disco." % p["name"],
                "Excluir", go, danger=True)

    # ---- operações -----------------------------------------------------------
    def do_backup(self):
        p = self.cur()
        if not p:
            return
        st = self.cfg["settings"]

        def done(kind, res):
            self.refresh()
            if kind == "ok":
                self.set_status("Backup concluído: %s (%s)" % (os.path.basename(res["path"]), fmt_size(res["csize"])), "ok")
            elif kind == "err":
                self.set_status("Falha no backup: %s" % res, "danger")
            else:
                self.set_status("Backup cancelado.")
        ProgressDialog(self, "Fazendo backup — %s" % p["name"],
                       lambda prog, cancel: run_backup(p, st, prog, cancel), done)

    def do_restore(self, b):
        p = self.cur()
        st = self.cfg["settings"]
        pastas = "\n".join("• " + s for s in p["sources"])
        extra = ("\n\nUm backup de segurança do estado atual será criado antes."
                 if st["safety_backup"] else "\n\nATENÇÃO: o backup de segurança está desativado.")

        def done(kind, res):
            self.refresh()
            if kind == "ok":
                self.set_status("Restauração concluída: %d arquivos." % res["files"], "ok")
            elif kind == "err":
                self.set_status("Falha na restauração: %s" % res, "danger")
            else:
                self.set_status("Restauração cancelada (pode estar parcial).", "danger")

        def go():
            ProgressDialog(self, "Restaurando — %s" % p["name"],
                           lambda prog, cancel: run_restore(p, st, b["path"], prog, cancel), done)
        confirm(self, "Restaurar backup",
                "Restaurar o backup de %s?\n\nOs arquivos nas pastas abaixo serão sobrescritos:\n%s%s" % (fmt_dt(b["ts"]), pastas, extra),
                "Restaurar", go)

    def edit_comment(self, b):
        m = Modal(self, "Comentário do backup", width=720)
        L(m.body, fmt_dt(b["ts"]) + "  •  " + b["fmt"].upper(), 14, "mut", bg=C["card"], anchor="w").pack(fill="x")
        var = tk.StringVar(value=b["comment"][:MAX_COMMENT])
        ent = mk_entry(m.body, var)
        ent.pack(fill="x", ipady=sz(1), pady=(sz(8), sz(4)))
        cnt = L(m.body, "", 13, "mut", bg=C["card"], anchor="e")
        cnt.pack(fill="x")
        err = L(m.body, "O comentário vai no final do nome do arquivo. Deixe vazio para remover.",
                13, "mut", bg=C["card"], anchor="w", wraplength=sz(660), justify="left")
        err.pack(fill="x", pady=(sz(4), 0))

        def on_change(*_a):
            if len(var.get()) > MAX_COMMENT:
                var.set(var.get()[:MAX_COMMENT])
            cnt.config(text="%d/%d" % (len(var.get()), MAX_COMMENT))
        var.trace_add("write", on_change)
        on_change()

        def save():
            try:
                set_comment(b, var.get())
            except OSError as e:
                err.config(text="⚠ " + str(e), fg=C["danger"])
                return
            m.close()
            self.refresh()
            self.set_status("Comentário atualizado.", "ok")
        ent.bind("<Return>", lambda e: save())
        m.buttons([("Cancelar", m.close, "ghost"), ("Salvar", save, "primary")])
        ent.focus_set()
        ent.select_range(0, "end")
        ent.icursor("end")

    def delete_backup(self, b):
        def go():
            try:
                os.remove(b["path"])
                self.set_status("Backup removido.")
            except OSError as e:
                self.set_status("Não foi possível remover: %s" % e, "danger")
            self.refresh()
        confirm(self, "Excluir backup", "Excluir o backup de %s (%s)?\n\nEsta ação não pode ser desfeita." % (fmt_dt(b["ts"]), fmt_size(b["size"])),
                "Excluir", go, danger=True)

    # ---- popups ------------------------------------------------------------
    def open_help(self):
        m = Modal(self, "Ajuda", width=760, dismiss=True)
        txt = ("1. Toque em “Novo perfil” e informe o jogo e as pastas onde ele guarda os saves.\n"
               "    Dica: no seletor, “Saves Proton” abre a pasta compatdata do Steam.\n\n"
               "2. Selecione o perfil e toque em “Fazer backup agora”.\n\n"
               "3. Para voltar a um backup, toque em “Restaurar” na linha dele. Antes de sobrescrever, "
               "o programa guarda o estado atual (backup de segurança).\n\n"
               "• Arraste a lista com o dedo para rolar.\n"
               "• Teclado virtual: Steam + X.   Tela cheia: F11.\n"
               "• Configuração: %s" % CFG_FILE)
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
        cpu = os.cpu_count() or 2

        def row(i, label, widget_fn, hint=""):
            lf = tk.Frame(g, bg=C["card"])
            lf.grid(row=i, column=0, sticky="w", pady=sz(5), padx=(0, sz(20)))
            L(lf, label, 15, "ink", bold=True, anchor="w", width=22).pack(anchor="w")
            if hint:
                L(lf, hint, 12, "mut", anchor="w").pack(anchor="w")
            w = widget_fn(g)
            w.grid(row=i, column=1, sticky="w", pady=sz(5))
        yn = [(True, "Sim"), (False, "Não")]
        row(0, "Tema", lambda p: Seg(p, [("dark", "Escuro"), ("light", "Claro")], st["theme"], lambda v: st.update(theme=v)))
        row(1, "Tela cheia", lambda p: Seg(p, yn, st["fullscreen"], lambda v: st.update(fullscreen=v), minw=90))
        row(2, "Formato padrão", lambda p: Seg(p, [("zip", "ZIP"), ("tar.gz", "TAR.GZ")], st["default_format"], lambda v: st.update(default_format=v)),
            "Para novos perfis")
        row(3, "Núcleos para compressão", lambda p: Stepper(p, min(st["workers"], cpu), 1, cpu, 1, lambda v: "%d de %d" % (v, cpu), lambda v: st.update(workers=v)))
        row(4, "Nível de compressão", lambda p: Stepper(p, st["level"], 1, 9, 1, lambda v: "%d" % v, lambda v: st.update(level=v)),
            "1 = rápido  •  9 = menor")
        row(5, "Backup de segurança", lambda p: Seg(p, yn, st["safety_backup"], lambda v: st.update(safety_backup=v), minw=90),
            "Antes de restaurar")
        row(6, "Escala da interface", lambda p: Stepper(p, st["scale"], 70, 160, 10, lambda v: "%d%%" % v, lambda v: st.update(scale=v)))

        def dirrow(p):
            f = tk.Frame(p, bg=C["card"])
            lbl = L(f, st["default_dir"], 13, "ink", mono=True, anchor="w", wraplength=sz(300), justify="left")

            def got(path):
                st["default_dir"] = path
                lbl.config(text=path)
            Btn(f, "Escolher…", lambda: FolderPicker(self, st["default_dir"], got, "Pasta padrão dos backups"),
                icon="folder").pack(side="right")
            lbl.pack(side="left", padx=(0, sz(10)))
            return f
        row(7, "Pasta padrão dos backups", dirrow)

        def close():
            m.close()
            save_config(self.cfg)
            self.apply_look()
            self.build_ui()
        m.buttons([("Fechar", close, "primary")])

    def run(self):
        self.root.mainloop()


def main():
    mp.freeze_support()
    App().run()


if __name__ == "__main__":
    main()

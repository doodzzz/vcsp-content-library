#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vcsp-upload - upload portal backend for the VCSP external content library.

Runs behind nginx on 127.0.0.1 as the unprivileged 'vcsp' user. nginx
terminates TLS, enforces administrator Basic authentication and passes the
authenticated user name in X-Remote-User. Python standard library only.

Upload protocol (resumable, no multipart parsing, constant memory):
  POST   /api/uploads                  {item, filename, size} -> {id, offset}
  PUT    /api/uploads/<id>             raw bytes, Content-Range: bytes a-b/total
  POST   /api/items/<item>/publish     {description?, mode: replace|merge} -> job
  GET    /api/jobs/<id>                job progress
  DELETE /api/items/<item>             remove an item (job)
  GET    /api/config | /api/items | /api/uploads | /api/health

State-changing requests must carry "X-VCSP-Request: 1"; browsers cannot add
that header cross-site without a CORS preflight this service never grants,
which blocks CSRF against the administrator's cached Basic credentials.
"""

import argparse
import fcntl
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import vcsp_validate as V  # noqa: E402

__version__ = "1.0.0"
META_FILE = ".vcsp-meta.json"
COPY_BUF = 1 << 20
log = logging.getLogger("vcsp-upload")


# ------------------------------------------------------------------ settings
def read_conf(path):
    conf = {}
    if not path or not os.path.isfile(path):
        return conf
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip().replace("export ", "", 1).strip()
            val = val.strip()
            if val[:1] in ("'", '"'):              # KEY="value"   # optional comment
                end = val.find(val[0], 1)
                val = val[1:end] if end > 0 else val[1:]
            else:                                   # KEY=value # optional comment
                val = val.split(" #", 1)[0].strip()
            conf[key] = val
    return conf


def conf_bool(value):
    return str(value).strip().lower() in ("1", "y", "yes", "true", "on")


class Settings:
    def __init__(self, conf, conf_path):
        g = conf.get
        self.conf_path = conf_path
        self.data_root = os.path.abspath(g("DATA_ROOT", "/srv/vcsp"))
        self.lib_root = os.path.join(self.data_root, "lib")
        self.staging = os.path.join(self.data_root, "staging")
        self.state_dir = os.path.abspath(g("STATE_DIR", "/var/lib/vcsp"))
        self.listen = g("UPLOAD_LISTEN", "127.0.0.1")
        self.port = int(g("UPLOAD_PORT", "8080"))
        allowed = []
        for ext in re.split(r"[\s,]+", g("ALLOWED_EXTENSIONS", "ovf vmdk mf cert nvram iso ova")):
            ext = ext.strip().lower().lstrip(".")
            if ext and ext not in allowed:
                allowed.append(ext)
        self.allowed = allowed
        self.chunk = min(max(int(g("UPLOAD_CHUNK_MB", "64")), 1), 512) << 20
        self.max_file = int(float(g("MAX_FILE_SIZE_GB", "256")) * (1 << 30))
        self.reserve = int(float(g("MIN_FREE_SPACE_GB", "20")) * (1 << 30))
        self.verify_mf = conf_bool(g("VERIFY_MANIFEST", "yes"))
        self.ova_extract = conf_bool(g("OVA_EXTRACT", "yes"))
        self.allow_delete = conf_bool(g("ALLOW_DELETE", "yes"))
        self.ttl = int(float(g("STAGING_TTL_HOURS", "72")) * 3600)
        self.hash_workers = int(g("HASH_WORKERS", "0") or 0) or min(4, os.cpu_count() or 1)
        self.lib_name = g("LIB_NAME", "VCSP Content Library")
        self.lib_auth = g("LIB_AUTH", "basic").lower()
        fqdn = g("SERVER_FQDN", "localhost")
        self.public_url = (g("PUBLIC_BASE_URL") or "https://" + fqdn).rstrip("/")
        self.index_bin = g("INDEX_BIN", "/opt/vcsp/bin/vcsp-index")


# ------------------------------------------------------------------ errors / jobs
class ApiError(Exception):
    def __init__(self, status, message, drain=False, **extra):
        super().__init__(message)
        self.status, self.message, self.drain, self.extra = status, message, drain, extra


class Job:
    def __init__(self, kind, item, user):
        self.id = uuid.uuid4().hex
        self.kind, self.item, self.user = kind, item, user
        self.state, self.steps, self.notes = "running", [], []
        self.error, self.result = None, None
        self.started, self.finished = time.time(), None

    def step(self, text):
        self.steps.append(text)
        log.info("job=%s item=%s user=%s step: %s", self.id[:8], self.item, self.user, text)

    def note(self, text):
        self.notes.append(text)

    def finish(self, result=None, error=None):
        self.result, self.error = result, error
        self.state = "failed" if error else "done"
        self.finished = time.time()
        log.info("job=%s kind=%s item=%s user=%s state=%s%s", self.id[:8], self.kind, self.item,
                 self.user, self.state, (" error=%s" % error) if error else "")

    def public(self):
        return {"id": self.id, "kind": self.kind, "item": self.item, "state": self.state,
                "steps": self.steps, "notes": self.notes, "error": self.error, "result": self.result}


# ------------------------------------------------------------------ portal
class Portal:
    def __init__(self, settings):
        self.s = settings
        self.uploads = os.path.join(settings.staging, "uploads")
        self.ready = os.path.join(settings.staging, "ready")
        self.trash = os.path.join(settings.staging, "trash")
        for path in (self.uploads, self.ready, self.trash):
            os.makedirs(path, mode=0o750, exist_ok=True)
        if not os.path.isdir(settings.lib_root):
            raise SystemExit("library folder %s does not exist" % settings.lib_root)
        self.same_fs = os.stat(settings.lib_root).st_dev == os.stat(settings.staging).st_dev
        if not self.same_fs:
            log.warning("%s and %s are on different filesystems: publishing will copy instead of rename",
                        settings.staging, settings.lib_root)
        self._locks, self._guard = {}, threading.Lock()
        self.jobs, self._jobs_guard = {}, threading.Lock()
        threading.Thread(target=self._janitor, name="janitor", daemon=True).start()

    # -------- helpers
    def lock_for(self, key):
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    @contextmanager
    def index_lock(self, timeout=900):
        """Same flock the indexer holds, so it never scans a half-swapped item."""
        fd = os.open(os.path.join(self.s.state_dir, "index.lock"), os.O_RDWR | os.O_CREAT, 0o660)
        deadline = time.monotonic() + timeout
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        raise V.ValidationError("The library index is busy; try publishing again shortly.")
                    time.sleep(0.2)
            yield
        finally:
            os.close(fd)

    def free_bytes(self):
        st = os.statvfs(self.s.staging)
        return st.f_bavail * st.f_frsize

    def read_state(self):
        try:
            with open(os.path.join(self.s.state_dir, "state.json"), encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    @staticmethod
    def list_files(path):
        try:
            with os.scandir(path) as it:
                return sorted(e.name for e in it if e.is_file(follow_symlinks=False)
                              and not e.name.startswith(".") and e.name != "item.json")
        except FileNotFoundError:
            return []

    def check_item(self, item):
        try:
            return V.check_item_name(item)
        except V.ValidationError as exc:
            raise ApiError(400, str(exc))

    def upload_paths(self, uid):
        return os.path.join(self.uploads, uid + ".part"), os.path.join(self.uploads, uid + ".json")

    def load_meta(self, uid):
        try:
            with open(self.upload_paths(uid)[1], encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    # -------- read-only API
    def config(self, user):
        lib = self.read_state().get("lib") or {}
        return {
            "lib_name": self.s.lib_name,
            "subscription_url": self.s.public_url + "/lib/lib.json",
            "lib_auth": self.s.lib_auth,
            "lib_version": lib.get("version"),
            "allowed_extensions": self.s.allowed,
            "chunk_size": self.s.chunk,
            "max_file_size": self.s.max_file,
            "free_bytes": self.free_bytes(),
            "reserve_bytes": self.s.reserve,
            "verify_manifest": self.s.verify_mf,
            "ova_extract": self.s.ova_extract,
            "allow_delete": self.s.allow_delete,
            "name_pattern": V.NAME_RE.pattern,
            "user": user,
            "version": __version__,
        }

    def items(self):
        state = self.read_state()
        by_dir = {}
        for key, rec in (state.get("items") or {}).items():
            by_dir.setdefault(rec.get("dir"), []).append((key, rec))
        out = []
        with os.scandir(self.s.lib_root) as it:
            dirs = sorted(e.name for e in it if e.is_dir(follow_symlinks=False)
                          and not e.name.startswith(".") and e.name != "lost+found")
        for name in dirs:
            files, newest, description = [], 0, ""
            with os.scandir(os.path.join(self.s.lib_root, name)) as it:
                for e in it:
                    if e.name == META_FILE:
                        try:
                            with open(e.path, encoding="utf-8") as fh:
                                description = str(json.load(fh).get("description", ""))
                        except (OSError, ValueError, AttributeError):
                            pass
                    if e.name.startswith(".") or e.name == "item.json" or not e.is_file(follow_symlinks=False):
                        continue
                    st = e.stat(follow_symlinks=False)
                    files.append({"name": e.name, "size": st.st_size})
                    newest = max(newest, st.st_mtime)
            recs = sorted(by_dir.get(name, []))
            entry = {"name": name, "files": sorted(files, key=lambda f: f["name"]),
                     "size": sum(f["size"] for f in files), "updated": int(newest),
                     "description": description, "children": []}
            if not recs:
                entry.update(type=None, version=None, status="pending",
                             reason="Not indexed yet; the index updates within a few minutes.")
            elif len(recs) == 1 and recs[0][0] == name:
                rec = recs[0][1]
                entry.update(type=rec.get("type"), version=rec.get("version"),
                             status=rec.get("status"), reason=rec.get("reason", ""))
            else:
                children = [{"name": rec.get("name"), "version": rec.get("version"),
                             "status": rec.get("status"), "reason": rec.get("reason", "")} for _k, rec in recs]
                worst = next((s for s in ("incomplete", "stale", "deferred")
                              if any(c["status"] == s for c in children)), "published")
                entry.update(type="vcsp.iso", version=None, status=worst, children=children,
                             reason="; ".join("%s: %s" % (c["name"], c["reason"]) for c in children if c["reason"]))
            out.append(entry)
        return {"items": out, "lib_version": (state.get("lib") or {}).get("version")}

    def uploads_list(self):
        pending = []
        for name in sorted(os.listdir(self.uploads)):
            if not name.endswith(".json"):
                continue
            uid = name[:-5]
            meta = self.load_meta(uid)
            part = self.upload_paths(uid)[0]
            if meta and os.path.exists(part):
                pending.append({"id": uid, "item": meta["item"], "filename": meta["filename"],
                                "size": meta["size"], "offset": os.path.getsize(part),
                                "user": meta.get("user"), "updated": int(os.path.getmtime(part))})
        ready = []
        for item in sorted(os.listdir(self.ready)):
            path = os.path.join(self.ready, item)
            files = [{"name": n, "size": os.path.getsize(os.path.join(path, n))} for n in self.list_files(path)]
            if files:
                ready.append({"item": item, "files": files})
        return {"uploads": pending, "ready": ready}

    # -------- uploads
    def upload_init(self, body, user):
        item = self.check_item(str(body.get("item", "")).strip())
        filename = str(body.get("filename", "")).strip()
        try:
            ext = V.check_file_name(filename, self.s.allowed)
        except V.ValidationError as exc:
            raise ApiError(400, str(exc))
        size = body.get("size")
        if not isinstance(size, int) or size <= 0:
            raise ApiError(400, "%s is empty." % filename)
        if size > self.s.max_file:
            raise ApiError(413, "%s is %.1f GiB; the limit is %.0f GiB (MAX_FILE_SIZE_GB)."
                           % (filename, size / (1 << 30), self.s.max_file / (1 << 30)))
        uid = V.new_hash("sha256")
        uid.update(("%s\0%s\0%d" % (item, filename, size)).encode())
        uid = uid.hexdigest()[:32]

        ready_path = os.path.join(self.ready, item, filename)
        if os.path.isfile(ready_path) and os.path.getsize(ready_path) == size:
            return {"id": uid, "offset": size, "complete": True, "chunk_size": self.s.chunk}

        part, meta_path = self.upload_paths(uid)
        with self.lock_for(uid):
            offset = os.path.getsize(part) if os.path.exists(part) else 0
            if offset > size:
                os.truncate(part, 0)
                offset = 0
            need = (size - offset) + (size if ext == "ova" and self.s.ova_extract else 0)
            free = self.free_bytes()
            if free - need < self.s.reserve:
                raise ApiError(507, "Not enough space for %s: it needs %.1f GiB and %.1f GiB is free after the "
                               "%.0f GiB reserve (MIN_FREE_SPACE_GB)." % (filename, need / (1 << 30),
                                                                         max(0, free - self.s.reserve) / (1 << 30),
                                                                         self.s.reserve / (1 << 30)))
            if not os.path.exists(meta_path):
                with open(meta_path, "w", encoding="utf-8") as fh:
                    json.dump({"item": item, "filename": filename, "size": size, "user": user,
                               "created": int(time.time())}, fh)
            open(part, "ab").close()
        log.info("upload-init user=%s item=%s file=%s size=%d offset=%d", user, item, filename, size, offset)
        return {"id": uid, "offset": offset, "complete": False, "chunk_size": self.s.chunk}

    def upload_chunk(self, uid, start, end, total, length, rfile, user):
        meta = self.load_meta(uid)
        if not meta:
            raise ApiError(404, "Unknown upload; start it again.", drain=True)
        if total != meta["size"] or end >= total or length != end - start + 1:
            raise ApiError(400, "Content-Range does not match the upload.", drain=True)
        if length > self.s.chunk:
            raise ApiError(413, "Chunks are limited to %d MiB." % (self.s.chunk >> 20), drain=True)
        lock = self.lock_for(uid)
        if not lock.acquire(blocking=False):
            raise ApiError(409, "%s is already uploading from another window." % meta["filename"], drain=True)
        try:
            part = self.upload_paths(uid)[0]
            if not os.path.exists(part):
                raise ApiError(404, "Unknown upload; start it again.", drain=True)
            current = os.path.getsize(part)
            if start != current:
                raise ApiError(409, "Resume from byte %d." % current, drain=True, offset=current)
            written = self._copy(rfile, part, length)
            offset = current + written
            if written < length:
                raise ApiError(400, "The connection closed mid-chunk; resume from byte %d." % offset, offset=offset)
            complete = offset == total
            ext = V.ext_of(meta["filename"])
            if start == 0 or complete:
                reason = V.sniff(part, ext, offset, complete)
                if reason:
                    self._discard(uid)
                    log.warning("upload-rejected user=%s item=%s file=%s reason=%s",
                                user, meta["item"], meta["filename"], reason)
                    raise ApiError(422, "%s was rejected because %s." % (meta["filename"], reason))
            if complete:
                self._finalize(uid, meta, part, user)
            return {"offset": offset, "complete": complete}
        finally:
            lock.release()

    @staticmethod
    def _copy(rfile, path, length):
        buf = bytearray(COPY_BUF)
        view = memoryview(buf)
        remaining, written = length, 0
        with open(path, "ab", buffering=0) as out:
            while remaining:
                n = rfile.readinto(view[:min(remaining, COPY_BUF)])
                if not n:
                    break
                chunk = view[:n]
                while chunk:
                    chunk = chunk[out.write(chunk):]
                remaining -= n
                written += n
        return written

    def _finalize(self, uid, meta, part, user):
        with open(part, "rb") as fh:
            os.fsync(fh.fileno())
        dest = os.path.join(self.ready, meta["item"])
        os.makedirs(dest, mode=0o750, exist_ok=True)
        os.replace(part, os.path.join(dest, meta["filename"]))
        os.unlink(self.upload_paths(uid)[1])
        log.info("upload-complete user=%s item=%s file=%s size=%d", user, meta["item"], meta["filename"], meta["size"])

    def _discard(self, uid):
        for path in self.upload_paths(uid):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def upload_cancel(self, uid, user):
        with self.lock_for(uid):
            meta = self.load_meta(uid) or {}
            self._discard(uid)
        log.info("upload-cancel user=%s item=%s file=%s", user, meta.get("item"), meta.get("filename"))
        return {"cancelled": uid}

    def staged_discard(self, item, user):
        self.check_item(item)
        shutil.rmtree(os.path.join(self.ready, item), ignore_errors=True)
        log.info("staged-discard user=%s item=%s", user, item)
        return {"discarded": item}

    # -------- publish / delete (background jobs)
    def _start_job(self, kind, item, user, target, *args):
        lock = self.lock_for("item:" + item)
        if not lock.acquire(blocking=False):
            raise ApiError(409, "Another publish or delete is running for %s." % item)
        job = Job(kind, item, user)
        with self._jobs_guard:
            cutoff = time.time() - 3600
            for jid in [j for j, v in self.jobs.items() if v.finished and v.finished < cutoff]:
                del self.jobs[jid]
            self.jobs[job.id] = job

        def runner():
            try:
                job.finish(result=target(job, item, *args))
            except V.ValidationError as exc:
                job.finish(error=str(exc))
            except Exception:
                log.exception("job %s failed", job.id)
                job.finish(error="Unexpected error; see 'journalctl -u vcsp-upload' on the server.")
            finally:
                lock.release()

        threading.Thread(target=runner, name="job-" + job.id[:8], daemon=True).start()
        return job.public()

    def publish(self, item, body, user):
        self.check_item(item)
        mode = body.get("mode", "replace")
        if mode not in ("replace", "merge"):
            raise ApiError(400, "mode must be replace or merge.")
        description = None
        if "description" in body and body["description"] is not None:
            description = str(body["description"]).strip()[:2000]
        return self._start_job("publish", item, user, self._publish, description, mode)

    def delete_item(self, item, user):
        if not self.s.allow_delete:
            raise ApiError(403, "Deleting items is disabled on this server (ALLOW_DELETE=no).")
        self.check_item(item)
        if not os.path.isdir(os.path.join(self.s.lib_root, item)):
            raise ApiError(404, "%s is not in the library." % item)
        return self._start_job("delete", item, user, self._delete)

    def _publish(self, job, item, description, mode):
        rdir = os.path.join(self.ready, item)
        ldir = os.path.join(self.s.lib_root, item)
        exists = os.path.isdir(ldir)
        staged = self.list_files(rdir)
        if not staged and description is None:
            raise V.ValidationError("Nothing to publish: upload the files for %s first." % item)
        if not staged and not exists:
            raise V.ValidationError("%s is not in the library yet; upload its files first." % item)

        for name in [n for n in staged if V.ext_of(n) == "ova"]:
            if not self.s.ova_extract:
                job.note("%s is kept as a plain file (OVA_EXTRACT=no)." % name)
                continue
            job.step("Extracting %s" % name)
            names = V.extract_ova(os.path.join(rdir, name), rdir, self.s.allowed)
            os.unlink(os.path.join(rdir, name))
            job.note("%s contained %s." % (name, ", ".join(names)))
        staged = self.list_files(rdir)

        final = {}
        if exists and (mode == "merge" or not staged):
            final.update({n: os.path.join(ldir, n) for n in self.list_files(ldir)})
        final.update({n: os.path.join(rdir, n) for n in staged})
        kind = None
        if staged:
            job.step("Checking the file set")
            kind, notes = V.validate_package(final)
            for note in notes:
                job.note(note)
            if self.s.verify_mf:
                for mf in sorted(n for n in final if V.ext_of(n) == "mf"):
                    job.step("Verifying checksums in %s" % mf)
                    problems = V.verify_manifest(final[mf], final.get, self.s.hash_workers)
                    if problems:
                        raise V.ValidationError("Checksum verification failed: " + "; ".join(problems[:5]) + ".")

        job.step("Publishing to the library")
        trash = None
        with self.index_lock():
            if not staged:
                self._write_meta(ldir, description)
            elif not exists:
                if description is not None:
                    self._write_meta(rdir, description)
                self._move(rdir, ldir)
            elif mode == "replace":
                old_meta = os.path.join(ldir, META_FILE)
                if description is not None:
                    self._write_meta(rdir, description)
                elif os.path.isfile(old_meta):
                    shutil.copy2(old_meta, os.path.join(rdir, META_FILE))
                trash = os.path.join(self.trash, "%s.%d.%s" % (item, time.time(), uuid.uuid4().hex[:6]))
                self._move(ldir, trash)
                self._move(rdir, ldir)
            else:
                for name in staged:
                    self._move(os.path.join(rdir, name), os.path.join(ldir, name))
                if description is not None:
                    self._write_meta(ldir, description)
                shutil.rmtree(rdir, ignore_errors=True)
            os.chmod(ldir, 0o755)
        if trash:
            threading.Thread(target=shutil.rmtree, args=(trash, True), daemon=True).start()

        job.step("Updating the library index")
        summary = self._run_indexer(item)
        statuses = {k: v for k, v in summary.get("item_status", {}).items() if k == item or k.startswith(item + "/")}
        problems = ["%s: %s" % (k, v["reason"]) for k, v in statuses.items() if v["status"] != "published"]
        for p in problems:
            job.note("Not published yet - " + p)
        versions = sorted({str(v["version"]) for v in statuses.values() if v.get("version")})
        return {"item": item, "kind": kind, "lib_version": summary.get("lib_version"),
                "item_versions": versions, "statuses": statuses}

    def _delete(self, job, item):
        ldir = os.path.join(self.s.lib_root, item)
        trash = os.path.join(self.trash, "%s.%d.%s" % (item, time.time(), uuid.uuid4().hex[:6]))
        job.step("Removing %s from the library" % item)
        with self.index_lock():
            self._move(ldir, trash)
        job.step("Updating the library index")
        summary = self._run_indexer(item)
        shutil.rmtree(trash, ignore_errors=True)
        return {"item": item, "lib_version": summary.get("lib_version")}

    def _move(self, src, dst):
        try:
            os.replace(src, dst) if os.path.isfile(src) else os.rename(src, dst)
        except OSError as exc:
            if exc.errno != 18:  # EXDEV: staging on another filesystem
                raise
            shutil.move(src, dst)

    @staticmethod
    def _write_meta(directory, description):
        path = os.path.join(directory, META_FILE)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"description": description or ""}, fh)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)

    def _run_indexer(self, item):
        cmd = [sys.executable, self.s.index_bin, "--config", self.s.conf_path,
               "--only", item, "--settle", "0", "--wait", "900", "--json"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        if proc.returncode not in (0, 4):
            raise V.ValidationError("The index update failed (exit %d): %s"
                                    % (proc.returncode, proc.stderr.strip()[-400:]))
        return json.loads(proc.stdout)

    def job(self, jid):
        with self._jobs_guard:
            job = self.jobs.get(jid)
        if not job:
            raise ApiError(404, "Unknown job.")
        return job.public()

    # -------- housekeeping
    def _janitor(self):
        while True:
            try:
                now = time.time()
                for name in os.listdir(self.uploads):
                    path = os.path.join(self.uploads, name)
                    if name.endswith(".part") and now - os.path.getmtime(path) > self.s.ttl:
                        self._discard(name[:-5])
                        log.info("janitor: removed abandoned upload %s", name)
                for item in os.listdir(self.ready):
                    path = os.path.join(self.ready, item)
                    if now - os.path.getmtime(path) > self.s.ttl:
                        shutil.rmtree(path, ignore_errors=True)
                        log.info("janitor: removed unpublished files for %s", item)
                for name in os.listdir(self.trash):
                    path = os.path.join(self.trash, name)
                    if now - os.path.getmtime(path) > 600:
                        shutil.rmtree(path, ignore_errors=True)
            except OSError as exc:
                log.warning("janitor: %s", exc)
            time.sleep(1800)


# ------------------------------------------------------------------ HTTP layer
ID = r"([0-9a-f]{32})"
ITEM = r"([^/]{1,80})"
ROUTES = [
    ("GET", re.compile(r"^/api/health$"), "health"),
    ("GET", re.compile(r"^/api/config$"), "config"),
    ("GET", re.compile(r"^/api/items$"), "items"),
    ("GET", re.compile(r"^/api/uploads$"), "uploads"),
    ("POST", re.compile(r"^/api/uploads$"), "upload_init"),
    ("PUT", re.compile(r"^/api/uploads/%s$" % ID), "upload_chunk"),
    ("DELETE", re.compile(r"^/api/uploads/%s$" % ID), "upload_cancel"),
    ("DELETE", re.compile(r"^/api/staged/%s$" % ITEM), "staged_discard"),
    ("POST", re.compile(r"^/api/items/%s/publish$" % ITEM), "publish"),
    ("DELETE", re.compile(r"^/api/items/%s$" % ITEM), "delete_item"),
    ("GET", re.compile(r"^/api/jobs/%s$" % ID), "job"),
]
RANGE_RE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")
USER_RE = re.compile(r"[^A-Za-z0-9._@\\-]")


class Handler(BaseHTTPRequestHandler):
    server_version = "vcsp-upload"
    sys_version = ""
    protocol_version = "HTTP/1.0"
    timeout = 300
    portal = None

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def log_message(self, fmt, *args):  # nginx already writes the access log
        log.debug("%s %s", self.address_string(), fmt % args)

    def _user(self):
        return USER_RE.sub("", self.headers.get("X-Remote-User", ""))[:64] or "-"

    def _dispatch(self, method):
        path = urlsplit(self.path).path
        self._body_left = int(self.headers.get("Content-Length") or 0) if method in ("POST", "PUT") else 0
        try:
            if method != "GET":
                if self.headers.get("X-VCSP-Request") != "1":
                    raise ApiError(403, "Missing X-VCSP-Request header.", drain=True)
                origin = self.headers.get("Origin")
                host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host")
                if origin and urlsplit(origin).netloc != host:
                    raise ApiError(403, "Cross-origin request refused.", drain=True)
            for verb, rx, name in ROUTES:
                match = rx.match(path) if verb == method else None
                if match:
                    args = [unquote(g) for g in match.groups()]
                    self._send(200, getattr(self, "h_" + name)(*args))
                    return
            raise ApiError(404 if not any(rx.match(path) for _v, rx, _n in ROUTES) else 405,
                           "No such endpoint.", drain=True)
        except ApiError as exc:
            if exc.drain:
                self._drain()
            self._send(exc.status, dict(exc.extra, error=exc.message))
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            log.info("client went away during %s %s", method, path)
        except Exception:
            log.exception("unhandled error on %s %s", method, path)
            try:
                self._send(500, {"error": "Unexpected server error; see 'journalctl -u vcsp-upload'."})
            except OSError:
                pass

    def _drain(self):
        """Consume an unread request body so nginx relays our error instead of a 502."""
        left = self._body_left
        if left > (600 << 20):
            self.close_connection = True
            return
        while left > 0:
            data = self.rfile.read(min(left, COPY_BUF))
            if not data:
                break
            left -= len(data)
        self._body_left = 0

    def _json(self, limit=65536):
        n = self._body_left
        if n > limit:
            raise ApiError(413, "Request body too large.", drain=True)
        raw = self.rfile.read(n) if n else b""
        self._body_left = 0
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            raise ApiError(400, "Request body must be JSON.")
        if not isinstance(body, dict):
            raise ApiError(400, "Request body must be a JSON object.")
        return body

    def _send(self, status, obj):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    # -------- handlers
    def h_health(self):
        return {"ok": True, "version": __version__}

    def h_config(self):
        return self.portal.config(self._user())

    def h_items(self):
        return self.portal.items()

    def h_uploads(self):
        return self.portal.uploads_list()

    def h_upload_init(self):
        return self.portal.upload_init(self._json(), self._user())

    def h_upload_chunk(self, uid):
        match = RANGE_RE.match(self.headers.get("Content-Range", ""))
        if not match or "Content-Length" not in self.headers:
            raise ApiError(400, "PUT needs Content-Length and Content-Range: bytes start-end/total.", drain=True)
        start, end, total = (int(x) for x in match.groups())
        length = self._body_left
        result = self.portal.upload_chunk(uid, start, end, total, length, self.rfile, self._user())
        self._body_left = 0
        return result

    def h_upload_cancel(self, uid):
        return self.portal.upload_cancel(uid, self._user())

    def h_staged_discard(self, item):
        return self.portal.staged_discard(item, self._user())

    def h_publish(self, item):
        return self.portal.publish(item, self._json(), self._user())

    def h_delete_item(self, item):
        return self.portal.delete_item(item, self._user())

    def h_job(self, jid):
        return self.portal.job(jid)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64


def main(argv=None):
    ap = argparse.ArgumentParser(description="VCSP content library upload portal backend")
    ap.add_argument("--config", default="/etc/vcsp/vcsp.conf")
    ap.add_argument("--listen", help="override UPLOAD_LISTEN")
    ap.add_argument("--port", type=int, help="override UPLOAD_PORT")
    ap.add_argument("--allow-root", action="store_true", help="permit running as root (testing only)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s", stream=sys.stderr)
    if os.geteuid() == 0 and not args.allow_root:
        raise SystemExit("Refusing to run as root; the service runs as the 'vcsp' user.")
    os.umask(0o022)
    settings = Settings(read_conf(args.config), args.config)
    if args.listen:
        settings.listen = args.listen
    if args.port:
        settings.port = args.port
    Handler.portal = Portal(settings)
    server = Server((settings.listen, settings.port), Handler)
    log.info("vcsp-upload %s listening on %s:%d, library %s, allowed extensions: %s",
             __version__, settings.listen, settings.port, settings.lib_root, " ".join(settings.allowed))
    try:
        server.serve_forever(poll_interval=1.0)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

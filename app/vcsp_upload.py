#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vcsp-upload - multi-tenant upload portal backend for the VCSP content library.

Runs behind nginx as the unprivileged 'vcsp' user and serves the provider
library plus any number of tenant libraries. nginx terminates TLS, checks
each tenant's own administrator passwords, and stamps every API request with:

  X-VCSP-Tenant     the scope nginx authenticated ("_provider" or a tenant name);
                    nginx overwrites whatever the client sent, so it cannot be spoofed
  X-VCSP-Proxy-Key  a secret only nginx and this service know, so local users
                    cannot bypass nginx by calling the loopback port directly
  X-Remote-User     the authenticated administrator, for the audit trail

Everything a request can touch (library, staging, index state, locks, jobs,
quota) is derived from that scope, so one tenant can never read or change
another tenant's content. Python standard library only.

Upload protocol (resumable, no multipart parsing, constant memory):
  POST   /api/uploads                  {item, filename, size} -> {id, offset}
  PUT    /api/uploads/<id>             raw bytes, Content-Range: bytes a-b/total
  POST   /api/items/<item>/publish     {description?, mode: replace|merge} -> job
  GET    /api/jobs/<id>                job progress
  DELETE /api/items/<item>             remove an item (job)
  GET    /api/config | /api/items | /api/uploads | /api/health

S3 import (only when the provider lists endpoints in S3_ALLOWED_ENDPOINTS):
  POST   /api/s3/list                  {endpoint, region, bucket, prefix, keys...} -> OVA/ISO objects
  POST   /api/s3/import                {..., key, item, description?, mode} -> job (download + publish)
  DELETE /api/jobs/<id>                cancel a running import
Tenant credentials for S3 are used for that one call or job and never stored.
"""

import argparse
import fcntl
import hmac
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
import vcsp_s3 as S3  # noqa: E402

__version__ = "2.3.0"
META_FILE = ".vcsp-meta.json"
COPY_BUF = 1 << 20
PROVIDER = "_provider"
TENANT_RE = re.compile(r"^[a-z][a-z0-9-]{1,30}[a-z0-9]$")
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
        self.state_dir = os.path.abspath(g("STATE_DIR", "/var/lib/vcsp"))
        self.tenant_conf_dir = os.path.abspath(g("TENANT_CONF_DIR", "/etc/vcsp/tenants"))
        self.proxy_key_file = g("PROXY_KEY_FILE", "/etc/vcsp/proxy.key")
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
        self.s3_endpoints = []
        for ep in re.split(r"[\s,]+", g("S3_ALLOWED_ENDPOINTS", "")):
            ep = ep.strip().rstrip("/")
            if ep and S3.ENDPOINT_RE.match(ep) and ep not in self.s3_endpoints:
                self.s3_endpoints.append(ep)
        self.s3_region = g("S3_DEFAULT_REGION", "us-east-1")
        self.s3_addressing = g("S3_ADDRESSING", "path")
        self.s3_ca_file = g("S3_CA_FILE", "") or None

    def load_proxy_key(self):
        try:
            with open(self.proxy_key_file, encoding="utf-8") as fh:
                key = fh.read().strip()
        except OSError as exc:
            raise SystemExit("cannot read the proxy key %s (%s); run 'deploy.sh install'" % (self.proxy_key_file, exc))
        if len(key) < 32:
            raise SystemExit("the proxy key in %s is too short" % self.proxy_key_file)
        return key


# ------------------------------------------------------------------ scopes
class Scope:
    """Everything one tenant (or the provider library) may touch."""

    def __init__(self, settings, tenant, tconf=None, stamp=None):
        self.tenant, self.stamp = tenant, stamp
        tconf = tconf or {}
        if tenant == PROVIDER:
            self.lib_root = os.path.join(settings.data_root, "lib")
            staging = os.path.join(settings.data_root, "staging")
            self.state_dir = settings.state_dir
            self.lib_name = settings.lib_name
            self.url_path = "/lib/lib.json"
            self.quota = 0
            self.state = "active"
            self.s3_endpoint = self.s3_bucket = ""
        else:
            base = os.path.join(settings.data_root, "tenants", tenant)
            self.lib_root = os.path.join(base, "lib")
            staging = os.path.join(base, "staging")
            self.state_dir = os.path.join(settings.state_dir, "tenants", tenant)
            self.lib_name = tconf.get("TENANT_DISPLAY_NAME") or tenant
            self.url_path = "/tenants/%s/lib/lib.json" % tenant
            self.quota = int(float(tconf.get("TENANT_QUOTA_GB", "0") or 0) * (1 << 30))
            self.state = tconf.get("TENANT_STATE", "active")
            self.s3_endpoint = tconf.get("TENANT_S3_ENDPOINT", "").rstrip("/")
            self.s3_bucket = tconf.get("TENANT_S3_BUCKET", "")
        self.staging = staging
        self.uploads = os.path.join(staging, "uploads")
        self.ready = os.path.join(staging, "ready")
        self.trash = os.path.join(staging, "trash")

    @property
    def label(self):
        return "provider" if self.tenant == PROVIDER else self.tenant

    def ensure_dirs(self):
        if not os.path.isdir(self.lib_root):
            raise FileNotFoundError(self.lib_root)
        for path in (self.uploads, self.ready, self.trash):
            os.makedirs(path, mode=0o750, exist_ok=True)


class ScopeRegistry:
    """Resolves tenant names to scopes; re-reads tenant.conf when it changes (no restart needed)."""

    def __init__(self, settings):
        self.s = settings
        self._cache, self._lock = {}, threading.Lock()

    def get(self, tenant):
        if tenant == PROVIDER:
            with self._lock:
                if PROVIDER not in self._cache:
                    self._cache[PROVIDER] = Scope(self.s, PROVIDER)
                return self._cache[PROVIDER]
        if not TENANT_RE.match(tenant or ""):
            return None
        path = os.path.join(self.s.tenant_conf_dir, tenant, "tenant.conf")
        try:
            stamp = os.stat(path).st_mtime_ns
        except OSError:
            with self._lock:
                self._cache.pop(tenant, None)
            return None
        with self._lock:
            scope = self._cache.get(tenant)
            if scope is None or scope.stamp != stamp:
                scope = Scope(self.s, tenant, read_conf(path), stamp)
                self._cache[tenant] = scope
            return scope

    def all(self):
        scopes = [self.get(PROVIDER)]
        try:
            names = sorted(os.listdir(self.s.tenant_conf_dir))
        except OSError:
            names = []
        for name in names:
            scope = self.get(name)
            if scope is not None:
                scopes.append(scope)
        return scopes


def human(n):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0


def tree_size(*roots):
    total = 0
    for root in roots:
        for dirpath, _dirs, files in os.walk(root):
            for name in files:
                try:
                    total += os.lstat(os.path.join(dirpath, name)).st_size
                except OSError:
                    pass
    return total


# ------------------------------------------------------------------ errors / jobs
class ApiError(Exception):
    def __init__(self, status, message, drain=False, **extra):
        super().__init__(message)
        self.status, self.message, self.drain, self.extra = status, message, drain, extra


class Job:
    def __init__(self, kind, tenant, item, user):
        self.id = uuid.uuid4().hex
        self.kind, self.tenant, self.item, self.user = kind, tenant, item, user
        self.state, self.steps, self.notes = "running", [], []
        self.error, self.result = None, None
        self.started, self.finished = time.time(), None
        self.progress, self.cancel_requested = None, False

    def _who(self):
        return "job=%s tenant=%s item=%s user=%s" % (self.id[:8], self.tenant, self.item, self.user)

    def step(self, text):
        self.steps.append(text)
        log.info("%s step: %s", self._who(), text)

    def note(self, text):
        self.notes.append(text)

    def finish(self, result=None, error=None):
        self.result, self.error = result, error
        self.state = "failed" if error else "done"
        self.finished = time.time()
        log.info("%s kind=%s state=%s%s", self._who(), self.kind, self.state, (" error=%s" % error) if error else "")

    def public(self):
        return {"id": self.id, "kind": self.kind, "item": self.item, "state": self.state,
                "steps": self.steps, "notes": self.notes, "error": self.error, "result": self.result,
                "progress": self.progress,
                "cancellable": self.kind == "s3-import" and self.state == "running" and self.progress is not None}


# ------------------------------------------------------------------ portal
class Portal:
    def __init__(self, settings):
        self.s = settings
        self.scopes = ScopeRegistry(settings)
        self.proxy_key = settings.load_proxy_key().encode()
        try:
            self.scopes.get(PROVIDER).ensure_dirs()
        except FileNotFoundError as exc:
            raise SystemExit("library folder %s does not exist" % exc)
        self._locks, self._guard = {}, threading.Lock()
        self.jobs, self._jobs_guard = {}, threading.Lock()
        threading.Thread(target=self._janitor, name="janitor", daemon=True).start()

    # -------- scope and helpers
    def scope(self, tenant):
        sc = self.scopes.get(tenant)
        if sc is None:
            raise ApiError(404, "Unknown tenant.", drain=True)
        if sc.state != "active":
            raise ApiError(403, "This tenant is suspended; contact the provider.", drain=True)
        try:
            sc.ensure_dirs()
        except FileNotFoundError:
            raise ApiError(503, "This tenant's library folder is missing; contact the provider.", drain=True)
        return sc

    def lock_for(self, key):
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    @contextmanager
    def index_lock(self, sc, timeout=900):
        """Same flock the indexer holds for this scope, so it never scans a half-swapped item."""
        os.makedirs(sc.state_dir, mode=0o750, exist_ok=True)
        fd = os.open(os.path.join(sc.state_dir, "index.lock"), os.O_RDWR | os.O_CREAT, 0o660)
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

    @staticmethod
    def free_bytes(sc):
        st = os.statvfs(sc.staging)
        return st.f_bavail * st.f_frsize

    @staticmethod
    def read_state(sc):
        try:
            with open(os.path.join(sc.state_dir, "state.json"), encoding="utf-8") as fh:
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

    @staticmethod
    def check_item(item):
        try:
            return V.check_item_name(item)
        except V.ValidationError as exc:
            raise ApiError(400, str(exc))

    @staticmethod
    def upload_paths(sc, uid):
        return os.path.join(sc.uploads, uid + ".part"), os.path.join(sc.uploads, uid + ".json")

    def load_meta(self, sc, uid):
        try:
            with open(self.upload_paths(sc, uid)[1], encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None

    # -------- read-only API
    def config(self, sc, user):
        lib = self.read_state(sc).get("lib") or {}
        return {
            "tenant": None if sc.tenant == PROVIDER else sc.tenant,
            "lib_name": sc.lib_name,
            "subscription_url": self.s.public_url + sc.url_path,
            "lib_auth": self.s.lib_auth,
            "lib_version": lib.get("version"),
            "allowed_extensions": self.s.allowed,
            "chunk_size": self.s.chunk,
            "max_file_size": self.s.max_file,
            "free_bytes": self.free_bytes(sc),
            "reserve_bytes": self.s.reserve,
            "quota_bytes": sc.quota or None,
            "used_bytes": tree_size(sc.lib_root, sc.staging) if sc.quota else None,
            "verify_manifest": self.s.verify_mf,
            "ova_extract": self.s.ova_extract,
            "allow_delete": self.s.allow_delete,
            "name_pattern": V.NAME_RE.pattern,
            "s3": {"enabled": bool(self.s3_import_allowed()), "endpoints": self.s.s3_endpoints,
                   "region": self.s.s3_region, "addressing": self.s.s3_addressing,
                   "suggested": ({"endpoint": sc.s3_endpoint, "bucket": sc.s3_bucket}
                                 if sc.s3_bucket and sc.s3_endpoint in self.s.s3_endpoints else None)},
            "user": user,
            "version": __version__,
        }

    def items(self, sc):
        state = self.read_state(sc)
        by_dir = {}
        for key, rec in (state.get("items") or {}).items():
            by_dir.setdefault(rec.get("dir"), []).append((key, rec))
        out = []
        with os.scandir(sc.lib_root) as it:
            dirs = sorted(e.name for e in it if e.is_dir(follow_symlinks=False)
                          and not e.name.startswith(".") and e.name != "lost+found")
        for name in dirs:
            files, newest, description = [], 0, ""
            with os.scandir(os.path.join(sc.lib_root, name)) as it:
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

    def uploads_list(self, sc):
        pending = []
        for name in sorted(os.listdir(sc.uploads)):
            if not name.endswith(".json"):
                continue
            uid = name[:-5]
            meta = self.load_meta(sc, uid)
            part = self.upload_paths(sc, uid)[0]
            if meta and os.path.exists(part):
                pending.append({"id": uid, "item": meta["item"], "filename": meta["filename"],
                                "size": meta["size"], "offset": os.path.getsize(part),
                                "user": meta.get("user"), "updated": int(os.path.getmtime(part))})
        ready = []
        for item in sorted(os.listdir(sc.ready)):
            path = os.path.join(sc.ready, item)
            files = [{"name": n, "size": os.path.getsize(os.path.join(path, n))} for n in self.list_files(path)]
            if files:
                ready.append({"item": item, "files": files})
        return {"uploads": pending, "ready": ready}

    # -------- uploads
    def upload_init(self, sc, body, user):
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
        uid.update(("%s\0%s\0%s\0%d" % (sc.tenant, item, filename, size)).encode())
        uid = uid.hexdigest()[:32]

        ready_path = os.path.join(sc.ready, item, filename)
        if os.path.isfile(ready_path) and os.path.getsize(ready_path) == size:
            return {"id": uid, "offset": size, "complete": True, "chunk_size": self.s.chunk}

        part, meta_path = self.upload_paths(sc, uid)
        with self.lock_for(sc.tenant + ":" + uid):
            offset = os.path.getsize(part) if os.path.exists(part) else 0
            if offset > size:
                os.truncate(part, 0)
                offset = 0
            need = (size - offset) + (size if ext == "ova" and self.s.ova_extract else 0)
            if sc.quota:
                used = tree_size(sc.lib_root, sc.staging)
                if used + need > sc.quota:
                    raise ApiError(507, "%s would exceed this tenant's storage quota: %s of %s is used and the "
                                   "upload needs %s more. Delete items or ask the provider for a larger quota."
                                   % (filename, human(used), human(sc.quota), human(need)))
            free = self.free_bytes(sc)
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
        log.info("upload-init tenant=%s user=%s item=%s file=%s size=%d offset=%d",
                 sc.label, user, item, filename, size, offset)
        return {"id": uid, "offset": offset, "complete": False, "chunk_size": self.s.chunk}

    def upload_chunk(self, sc, uid, start, end, total, length, rfile, user):
        meta = self.load_meta(sc, uid)
        if not meta:
            raise ApiError(404, "Unknown upload; start it again.", drain=True)
        if total != meta["size"] or end >= total or length != end - start + 1:
            raise ApiError(400, "Content-Range does not match the upload.", drain=True)
        if length > self.s.chunk:
            raise ApiError(413, "Chunks are limited to %d MiB." % (self.s.chunk >> 20), drain=True)
        lock = self.lock_for(sc.tenant + ":" + uid)
        if not lock.acquire(blocking=False):
            raise ApiError(409, "%s is already uploading from another window." % meta["filename"], drain=True)
        try:
            part = self.upload_paths(sc, uid)[0]
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
                    self._discard(sc, uid)
                    log.warning("upload-rejected tenant=%s user=%s item=%s file=%s reason=%s",
                                sc.label, user, meta["item"], meta["filename"], reason)
                    raise ApiError(422, "%s was rejected because %s." % (meta["filename"], reason))
            if complete:
                self._finalize(sc, uid, meta, part, user)
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

    def _finalize(self, sc, uid, meta, part, user):
        with open(part, "rb") as fh:
            os.fsync(fh.fileno())
        dest = os.path.join(sc.ready, meta["item"])
        os.makedirs(dest, mode=0o750, exist_ok=True)
        os.replace(part, os.path.join(dest, meta["filename"]))
        os.unlink(self.upload_paths(sc, uid)[1])
        log.info("upload-complete tenant=%s user=%s item=%s file=%s size=%d",
                 sc.label, user, meta["item"], meta["filename"], meta["size"])

    def _discard(self, sc, uid):
        for path in self.upload_paths(sc, uid):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def upload_cancel(self, sc, uid, user):
        with self.lock_for(sc.tenant + ":" + uid):
            meta = self.load_meta(sc, uid) or {}
            self._discard(sc, uid)
        log.info("upload-cancel tenant=%s user=%s item=%s file=%s", sc.label, user, meta.get("item"), meta.get("filename"))
        return {"cancelled": uid}

    def staged_discard(self, sc, item, user):
        self.check_item(item)
        shutil.rmtree(os.path.join(sc.ready, item), ignore_errors=True)
        log.info("staged-discard tenant=%s user=%s item=%s", sc.label, user, item)
        return {"discarded": item}

    # -------- publish / delete (background jobs)
    def _start_job(self, sc, kind, item, user, target, *args):
        lock = self.lock_for("item:%s:%s" % (sc.tenant, item))
        if not lock.acquire(blocking=False):
            raise ApiError(409, "Another publish or delete is running for %s." % item)
        job = Job(kind, sc.tenant, item, user)
        with self._jobs_guard:
            cutoff = time.time() - 3600
            for jid in [j for j, v in self.jobs.items() if v.finished and v.finished < cutoff]:
                del self.jobs[jid]
            self.jobs[job.id] = job

        def runner():
            try:
                job.finish(result=target(job, sc, item, *args))
            except V.ValidationError as exc:
                job.finish(error=str(exc))
            except Exception:
                log.exception("job %s failed", job.id)
                job.finish(error="Unexpected error; see 'journalctl -u vcsp-upload' on the server.")
            finally:
                lock.release()

        threading.Thread(target=runner, name="job-" + job.id[:8], daemon=True).start()
        return job.public()

    def publish(self, sc, item, body, user):
        self.check_item(item)
        mode = body.get("mode", "replace")
        if mode not in ("replace", "merge"):
            raise ApiError(400, "mode must be replace or merge.")
        description = None
        if "description" in body and body["description"] is not None:
            description = str(body["description"]).strip()[:2000]
        return self._start_job(sc, "publish", item, user, self._publish, description, mode)

    def delete_item(self, sc, item, user):
        if not self.s.allow_delete:
            raise ApiError(403, "Deleting items is disabled on this server (ALLOW_DELETE=no).")
        self.check_item(item)
        if not os.path.isdir(os.path.join(sc.lib_root, item)):
            raise ApiError(404, "%s is not in the library." % item)
        return self._start_job(sc, "delete", item, user, self._delete)

    def _publish(self, job, sc, item, description, mode):
        rdir = os.path.join(sc.ready, item)
        ldir = os.path.join(sc.lib_root, item)
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
        with self.index_lock(sc):
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
                trash = os.path.join(sc.trash, "%s.%d.%s" % (item, time.time(), uuid.uuid4().hex[:6]))
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
        summary = self._run_indexer(sc, item)
        statuses = {k: v for k, v in summary.get("item_status", {}).items() if k == item or k.startswith(item + "/")}
        for key, info in statuses.items():
            if info["status"] != "published":
                job.note("Not published yet - %s: %s" % (key, info["reason"]))
        versions = sorted({str(v["version"]) for v in statuses.values() if v.get("version")})
        return {"item": item, "kind": kind, "lib_version": summary.get("lib_version"),
                "item_versions": versions, "statuses": statuses}

    def _delete(self, job, sc, item):
        ldir = os.path.join(sc.lib_root, item)
        trash = os.path.join(sc.trash, "%s.%d.%s" % (item, time.time(), uuid.uuid4().hex[:6]))
        job.step("Removing %s from the library" % item)
        with self.index_lock(sc):
            self._move(ldir, trash)
        job.step("Updating the library index")
        summary = self._run_indexer(sc, item)
        shutil.rmtree(trash, ignore_errors=True)
        return {"item": item, "lib_version": summary.get("lib_version")}

    @staticmethod
    def _move(src, dst):
        try:
            if os.path.isfile(src):
                os.replace(src, dst)
            else:
                os.rename(src, dst)
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

    def _run_indexer(self, sc, item):
        cmd = [sys.executable, self.s.index_bin, "--config", self.s.conf_path]
        if sc.tenant != PROVIDER:
            cmd += ["--tenant", sc.tenant]
        cmd += ["--only", item, "--settle", "0", "--wait", "900", "--json"]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
        if proc.returncode not in (0, 4):
            raise V.ValidationError("The index update failed (exit %d): %s"
                                    % (proc.returncode, proc.stderr.strip()[-400:]))
        return json.loads(proc.stdout)

    def job(self, sc, jid):
        with self._jobs_guard:
            job = self.jobs.get(jid)
        if not job or job.tenant != sc.tenant:     # another tenant's job does not exist for you
            raise ApiError(404, "Unknown job.")
        return job.public()

    # -------- S3 import
    S3_EXTENSIONS = ("ova", "iso")

    def s3_import_allowed(self):
        return [e for e in self.S3_EXTENSIONS if e in self.s.allowed] if self.s.s3_endpoints else []

    def _s3_client(self, body):
        if not self.s3_import_allowed():
            raise ApiError(403, "Importing from S3 is not enabled on this server.")
        endpoint = str(body.get("endpoint", "")).strip().rstrip("/")
        if endpoint not in self.s.s3_endpoints:
            raise ApiError(400, "That S3 endpoint is not on the provider's list of allowed endpoints.")
        region = str(body.get("region") or self.s.s3_region).strip()
        addressing = str(body.get("addressing") or self.s.s3_addressing).strip()
        try:
            return S3.S3Client(endpoint, region, str(body.get("bucket", "")).strip(),
                               str(body.get("access_key", "")).strip(), str(body.get("secret_key", "")),
                               str(body.get("session_token", "")).strip() or None, addressing, self.s.s3_ca_file)
        except S3.S3Error as exc:
            raise ApiError(400, str(exc))

    @staticmethod
    def _s3_filename(key):
        """Object key -> a file name the library accepts (unsafe characters become hyphens)."""
        name = key.rsplit("/", 1)[-1]
        stem, _, ext = name.rpartition(".")
        stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._")[:190] or "image"
        if not stem[0].isalnum():
            stem = "i" + stem
        return "%s.%s" % (stem, ext.lower())

    def s3_list(self, sc, body, user):
        client = self._s3_client(body)
        prefix = str(body.get("prefix", ""))[:1024]
        token = str(body.get("continuation") or "")[:4096] or None
        try:
            page = client.list(prefix, token)
        except S3.S3Error as exc:
            raise ApiError(502, str(exc))
        wanted = self.s3_import_allowed()
        objects = [o for o in page["objects"] if V.ext_of(o["key"]) in wanted and o["size"] > 0]
        log.info("s3-list tenant=%s user=%s endpoint=%s bucket=%s prefix=%r scanned=%d matched=%d",
                 sc.label, user, client.endpoint, client.bucket, prefix, len(page["objects"]), len(objects))
        return {"objects": objects, "next": page["next"], "scanned": len(page["objects"])}

    def s3_import(self, sc, body, user):
        client = self._s3_client(body)
        key = str(body.get("key", ""))
        if not key or len(key.encode()) > 1024 or any(ord(ch) < 32 for ch in key):
            raise ApiError(400, "Choose an object to import.")
        ext = V.ext_of(key)
        if ext not in self.s3_import_allowed():
            raise ApiError(400, "Only %s objects can be imported from S3." % " and ".join("." + e for e in self.s3_import_allowed()))
        item = self.check_item(str(body.get("item", "")).strip())
        filename = self._s3_filename(key)
        try:
            V.check_file_name(filename, self.s.allowed)
        except V.ValidationError as exc:
            raise ApiError(400, str(exc))
        mode = body.get("mode", "replace")
        if mode not in ("replace", "merge"):
            raise ApiError(400, "mode must be replace or merge.")
        description = None
        if "description" in body and body["description"] is not None:
            description = str(body["description"]).strip()[:2000]
        if self.list_files(os.path.join(sc.ready, item)):
            raise ApiError(409, "Files are already staged for %s; publish or discard them before importing." % item)
        try:
            head = client.head(key)
        except S3.S3Error as exc:
            raise ApiError(502, str(exc))
        size = head["size"]
        if size <= 0:
            raise ApiError(400, "The object is empty.")
        if size > self.s.max_file:
            raise ApiError(413, "The object is %s; the limit is %s (MAX_FILE_SIZE_GB)." % (human(size), human(self.s.max_file)))
        need = size * (2 if ext == "ova" and self.s.ova_extract else 1)
        if sc.quota:
            used = tree_size(sc.lib_root, sc.staging)
            if used + need > sc.quota:
                raise ApiError(507, "Importing it would exceed this tenant's storage quota: %s of %s is used and the "
                               "import needs %s." % (human(used), human(sc.quota), human(need)))
        free = self.free_bytes(sc)
        if free - need < self.s.reserve:
            raise ApiError(507, "Not enough space: the import needs %s and %s is free after the reserve."
                           % (human(need), human(max(0, free - self.s.reserve))))
        log.info("s3-import tenant=%s user=%s endpoint=%s bucket=%s key=%r size=%d item=%s",
                 sc.label, user, client.endpoint, client.bucket, key, size, item)
        return self._start_job(sc, "s3-import", item, user, self._s3_import, client, key, filename, size, description, mode)

    def _s3_import(self, job, sc, item, client, key, filename, size, description, mode):
        job.step("Downloading %s from bucket %s (%s)" % (key, client.bucket, human(size)))
        job.progress = {"done": 0, "total": size}
        tmp = os.path.join(sc.uploads, "s3-%s.part" % job.id)
        try:
            integrity = client.download(key, tmp, size, progress=lambda d, t: job.__setattr__("progress", {"done": d, "total": t}),
                                        cancelled=lambda: job.cancel_requested)
        except S3.S3Cancelled:
            self._unlink(tmp)
            raise V.ValidationError("Import cancelled; nothing was published.")
        except S3.S3Error as exc:
            self._unlink(tmp)
            raise V.ValidationError("The S3 download failed: %s" % exc)
        except Exception:
            self._unlink(tmp)
            raise
        job.progress = None
        job.note("Checksum: MD5 matches the S3 ETag." if integrity["verified"] == "md5" else
                 "The S3 ETag is not a plain MD5 (multipart or KMS-encrypted object); the content is checked "
                 "by its file header and, for OVAs, the OVF manifest.")
        reason = V.sniff(tmp, V.ext_of(filename), size, True)
        if reason:
            self._unlink(tmp)
            raise V.ValidationError("%s was rejected because %s." % (filename, reason))
        dest = os.path.join(sc.ready, item)
        os.makedirs(dest, mode=0o750, exist_ok=True)
        os.replace(tmp, os.path.join(dest, filename))
        return self._publish(job, sc, item, description, mode)

    @staticmethod
    def _unlink(path):
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    def cancel_job(self, sc, jid):
        with self._jobs_guard:
            job = self.jobs.get(jid)
        if not job or job.tenant != sc.tenant:
            raise ApiError(404, "Unknown job.")
        if job.kind != "s3-import" or job.state != "running" or job.progress is None:
            raise ApiError(409, "Only an import that is still downloading can be cancelled.")
        job.cancel_requested = True
        log.info("s3-cancel tenant=%s item=%s job=%s", sc.label, job.item, job.id[:8])
        return job.public()

    # -------- housekeeping
    def _janitor(self):
        while True:
            for sc in self.scopes.all():
                try:
                    now = time.time()
                    if os.path.isdir(sc.uploads):
                        for name in os.listdir(sc.uploads):
                            path = os.path.join(sc.uploads, name)
                            if name.endswith(".part") and now - os.path.getmtime(path) > self.s.ttl:
                                self._discard(sc, name[:-5])
                                self._unlink(path)
                                log.info("janitor: tenant=%s removed abandoned upload %s", sc.label, name)
                    if os.path.isdir(sc.ready):
                        for item in os.listdir(sc.ready):
                            path = os.path.join(sc.ready, item)
                            if now - os.path.getmtime(path) > self.s.ttl:
                                shutil.rmtree(path, ignore_errors=True)
                                log.info("janitor: tenant=%s removed unpublished files for %s", sc.label, item)
                    if os.path.isdir(sc.trash):
                        for name in os.listdir(sc.trash):
                            path = os.path.join(sc.trash, name)
                            if now - os.path.getmtime(path) > 600:
                                shutil.rmtree(path, ignore_errors=True)
                except OSError as exc:
                    log.warning("janitor: tenant=%s %s", sc.label, exc)
            time.sleep(1800)


# ------------------------------------------------------------------ HTTP layer
ID = r"([0-9a-f]{32})"
ITEM = r"([^/]{1,80})"
ROUTES = [
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
    ("DELETE", re.compile(r"^/api/jobs/%s$" % ID), "job_cancel"),
    ("POST", re.compile(r"^/api/s3/list$"), "s3_list"),
    ("POST", re.compile(r"^/api/s3/import$"), "s3_import"),
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
            if method == "GET" and path == "/api/health":
                self._send(200, {"ok": True, "version": __version__})
                return
            key = self.headers.get("X-VCSP-Proxy-Key", "").encode()
            if not hmac.compare_digest(key, self.portal.proxy_key):
                raise ApiError(403, "Requests must come through the portal's web server.", drain=True)
            sc = self.portal.scope(self.headers.get("X-VCSP-Tenant", ""))
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
                    self._send(200, getattr(self, "h_" + name)(sc, *args))
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

    # -------- handlers (sc = the scope nginx authenticated)
    def h_config(self, sc):
        return self.portal.config(sc, self._user())

    def h_items(self, sc):
        return self.portal.items(sc)

    def h_uploads(self, sc):
        return self.portal.uploads_list(sc)

    def h_upload_init(self, sc):
        return self.portal.upload_init(sc, self._json(), self._user())

    def h_upload_chunk(self, sc, uid):
        match = RANGE_RE.match(self.headers.get("Content-Range", ""))
        if not match or "Content-Length" not in self.headers:
            raise ApiError(400, "PUT needs Content-Length and Content-Range: bytes start-end/total.", drain=True)
        start, end, total = (int(x) for x in match.groups())
        result = self.portal.upload_chunk(sc, uid, start, end, total, self._body_left, self.rfile, self._user())
        self._body_left = 0
        return result

    def h_upload_cancel(self, sc, uid):
        return self.portal.upload_cancel(sc, uid, self._user())

    def h_staged_discard(self, sc, item):
        return self.portal.staged_discard(sc, item, self._user())

    def h_publish(self, sc, item):
        return self.portal.publish(sc, item, self._json(), self._user())

    def h_delete_item(self, sc, item):
        return self.portal.delete_item(sc, item, self._user())

    def h_job(self, sc, jid):
        return self.portal.job(sc, jid)

    def h_job_cancel(self, sc, jid):
        return self.portal.cancel_job(sc, jid)

    def h_s3_list(self, sc):
        return self.portal.s3_list(sc, self._json(), self._user())

    def h_s3_import(self, sc):
        return self.portal.s3_import(sc, self._json(), self._user())


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64


def main(argv=None):
    ap = argparse.ArgumentParser(description="VCSP content library upload portal backend (multi-tenant)")
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
    log.info("vcsp-upload %s listening on %s:%d, provider library %s, tenants under %s, allowed extensions: %s",
             __version__, settings.listen, settings.port, os.path.join(settings.data_root, "lib"),
             os.path.join(settings.data_root, "tenants"), " ".join(settings.allowed))
    try:
        server.serve_forever(poll_interval=1.0)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

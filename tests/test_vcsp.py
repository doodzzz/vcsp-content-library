#!/usr/bin/env python3
"""
End-to-end tests for vcsp-index and the vcsp-upload service (stdlib only).

    python3 tests/test_vcsp.py -v

Builds a throw-away library in a temp folder, starts the upload service on a
free loopback port and drives it exactly like the browser portal does.
"""

import hashlib
import http.client
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
INDEX = os.path.join(ROOT, "bin", "vcsp-index")
SERVICE = os.path.join(ROOT, "app", "vcsp_upload.py")
MB = 1 << 20


def ovf_doc(refs, vapp=False):
    files = "".join('<File ovf:href="%s" ovf:id="f%d" ovf:size="%d"/>' % (h, i, s) for i, (h, s) in enumerate(refs))
    body = ('<VirtualSystemCollection ovf:id="app"><VirtualSystem ovf:id="vm"/></VirtualSystemCollection>'
            if vapp else '<VirtualSystem ovf:id="vm"/>')
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<Envelope xmlns="http://schemas.dmtf.org/ovf/envelope/1" '
            'xmlns:ovf="http://schemas.dmtf.org/ovf/envelope/1"><References>%s</References>%s</Envelope>\n'
            % (files, body)).encode()


def template(prefix="tmpl", disk_size=int(2.5 * MB)):
    disk = b"KDMV" + os.urandom(disk_size - 4)
    nvram = os.urandom(8684)
    ovf = ovf_doc([("%s-disk1.vmdk" % prefix, len(disk)), ("%s.nvram" % prefix, len(nvram))])
    files = {"%s.ovf" % prefix: ovf, "%s-disk1.vmdk" % prefix: disk, "%s.nvram" % prefix: nvram}
    files["%s.mf" % prefix] = "".join("SHA256(%s)= %s\n" % (n, hashlib.sha256(d).hexdigest())
                                      for n, d in files.items()).encode()
    return files


def iso_image(size=64 * 1024):
    data = bytearray(size)
    data[32769:32774] = b"CD001"
    return bytes(data)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Env:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="vcsp-test-")
        self.data = os.path.join(self.tmp, "data")
        self.lib = os.path.join(self.data, "lib")
        self.state = os.path.join(self.tmp, "state")
        os.makedirs(self.lib)
        os.makedirs(self.state)
        self.tenant_dir = os.path.join(self.tmp, "tenants")
        os.makedirs(self.tenant_dir)
        self.proxy_key = "k" * 16 + os.urandom(16).hex()
        key_file = os.path.join(self.tmp, "proxy.key")
        with open(key_file, "w") as fh:
            fh.write(self.proxy_key + "\n")
        self.conf = os.path.join(self.tmp, "vcsp.conf")
        with open(self.conf, "w") as fh:
            fh.write('LIB_NAME="Test Library"\nSERVER_FQDN="vcsp.test.local"\nDATA_ROOT="%s"\nSTATE_DIR="%s"\n'
                     'INDEX_BIN="%s"\nUPLOAD_CHUNK_MB="1"\nMIN_FREE_SPACE_GB="0"\nSETTLE_SECONDS="0"\n'
                     'TENANT_CONF_DIR="%s"\nPROXY_KEY_FILE="%s"\n'
                     % (self.data, self.state, INDEX, self.tenant_dir, key_file))

    def add_tenant(self, name, display=None, quota_gb=0, state="active"):
        """What 'deploy.sh tenant-add' creates, minus nginx and passwords."""
        os.makedirs(os.path.join(self.tenant_dir, name), exist_ok=True)
        with open(os.path.join(self.tenant_dir, name, "tenant.conf"), "w") as fh:
            fh.write('TENANT_NAME="%s"\nTENANT_DISPLAY_NAME="%s"\nTENANT_QUOTA_GB="%s"\nTENANT_STATE="%s"\n'
                     % (name, display or name, quota_gb, state))
        for sub in ("lib", "staging"):
            os.makedirs(os.path.join(self.data, "tenants", name, sub), exist_ok=True)
        os.makedirs(os.path.join(self.state, "tenants", name), exist_ok=True)

    def index(self, *extra):
        out = subprocess.run([sys.executable, INDEX, "--config", self.conf, "--json"] + list(extra),
                             capture_output=True, text=True)
        if out.returncode not in (0, 4):
            raise AssertionError(out.stderr)
        return json.loads(out.stdout)

    def lib_json(self, name="lib.json", tenant=None):
        root = self.lib if tenant is None else os.path.join(self.data, "tenants", tenant, "lib")
        with open(os.path.join(root, name)) as fh:
            return json.load(fh)

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class ConfigTests(unittest.TestCase):
    def test_shipped_config_parses_like_bash(self):
        """Python services must read config/vcsp.conf exactly as deploy.sh (bash 'source') does."""
        sys.path.insert(0, os.path.join(ROOT, "app"))
        import vcsp_upload
        path = os.path.join(ROOT, "config", "vcsp.conf")
        conf = vcsp_upload.read_conf(path)
        keys = sorted(conf)
        script = "set -a; source %s; " % path + "; ".join('printf "%%s\\0" "$%s"' % k for k in keys)
        values = subprocess.run(["bash", "-c", script], capture_output=True, text=True).stdout.split("\0")[:-1]
        self.assertEqual(dict(zip(keys, values)), conf)
        self.assertEqual(conf["STAGING_TTL_HOURS"], "72")
        settings = vcsp_upload.Settings(conf, path)          # must not raise
        self.assertIn("ova", settings.allowed)


class IndexerTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()

    def tearDown(self):
        self.env.cleanup()

    def write_item(self, item, files):
        path = os.path.join(self.env.lib, item)
        os.makedirs(path, exist_ok=True)
        for name, data in files.items():
            with open(os.path.join(path, name), "wb") as fh:
                fh.write(data)

    def items(self):
        return {i["name"]: i for i in self.env.lib_json("items.json")["items"]}

    def test_publish_types_and_versions(self):
        self.write_item("ubuntu", template("ubuntu"))
        self.write_item("media", {"win.iso": iso_image()})
        self.write_item("tools", {"a.iso": iso_image(), "b.iso": iso_image()})
        s = self.env.index()
        self.assertEqual(s["items"]["published"], 4)
        items = self.items()
        self.assertEqual(items["ubuntu"]["type"], "vcsp.ovf")
        self.assertEqual(items["media"]["type"], "vcsp.iso")
        self.assertEqual(items["a"]["selfHref"], "tools/a/item.json")
        self.assertTrue(os.path.isfile(os.path.join(self.env.lib, "tools", "a", "item.json")))
        v1 = int(self.env.lib_json()["version"])

        s = self.env.index()                              # no change -> no version bump
        self.assertEqual(s["unchanged"], 4)
        self.assertEqual(int(self.env.lib_json()["version"]), v1)

        time.sleep(1.1)
        with open(os.path.join(self.env.lib, "media", "win.iso"), "ab") as fh:
            fh.write(b"\0")
        self.env.index()
        self.assertEqual(self.items()["media"]["version"], "3")
        self.assertEqual(self.items()["media"]["contentVersion"], "3")
        self.assertEqual(int(self.env.lib_json()["version"]), v1 + 1)

        with open(os.path.join(self.env.lib, "media", ".vcsp-meta.json"), "w") as fh:
            json.dump({"description": "Windows media"}, fh)
        self.env.index()
        self.assertEqual(self.items()["media"]["version"], "4")        # description bumps version only
        self.assertEqual(self.items()["media"]["contentVersion"], "3")

    def test_incomplete_ovf_is_held_back(self):
        files = template("t")
        del files["t-disk1.vmdk"]
        self.write_item("t", files)
        s = self.env.index("--check")
        self.assertIn("t", s["incomplete"])
        self.assertNotIn("t", self.items())

    def test_crash_truncated_item_json_is_repaired(self):
        self.write_item("media", {"x.iso": iso_image()})
        self.env.index()
        path = os.path.join(self.env.lib, "media", "item.json")
        open(path, "w").close()
        self.env.index()
        with open(path) as fh:
            self.assertEqual(json.load(fh)["version"], "2")

    def test_ids_survive_lost_state(self):
        self.write_item("media", {"x.iso": iso_image()})
        self.env.index()
        before = (self.env.lib_json()["id"], self.items()["media"]["id"])
        os.unlink(os.path.join(self.env.state, "state.json"))
        self.env.index()
        self.assertEqual(before, (self.env.lib_json()["id"], self.items()["media"]["id"]))


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = Env()
        cls.port = free_port()
        cmd = [sys.executable, SERVICE, "--config", cls.env.conf, "--port", str(cls.port)]
        if os.geteuid() == 0:
            cmd.append("--allow-root")
        cls.proc = subprocess.Popen(cmd, stderr=subprocess.PIPE)
        for _ in range(50):
            try:
                if cls.call("GET", "/api/health")[0] == 200:
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("service did not start")

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)
        cls.env.cleanup()

    @classmethod
    def call(cls, method, path, body=None, headers=None, raw=None, tenant="_provider"):
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=30)
        hdrs = {"X-VCSP-Request": "1", "X-Remote-User": "tester",
                "X-VCSP-Proxy-Key": cls.env.proxy_key, "X-VCSP-Tenant": tenant}
        hdrs.update(headers or {})
        payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        if payload is not None and "Content-Type" not in hdrs:
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=payload, headers=hdrs)
        resp = conn.getresponse()
        data = json.loads(resp.read() or b"{}")
        conn.close()
        return resp.status, data

    def upload(self, item, name, data, tenant="_provider"):
        status, init = self.call("POST", "/api/uploads", {"item": item, "filename": name, "size": len(data)}, tenant=tenant)
        if status != 200:
            return status, init
        offset, chunk = init["offset"], init["chunk_size"]
        while offset < len(data):
            part = data[offset:offset + chunk]
            status, res = self.call("PUT", "/api/uploads/" + init["id"], raw=part, tenant=tenant, headers={
                "Content-Type": "application/octet-stream",
                "Content-Range": "bytes %d-%d/%d" % (offset, offset + len(part) - 1, len(data))})
            if status != 200:
                return status, res
            offset = res["offset"]
        return 200, {"offset": offset}

    def publish(self, item, tenant="_provider", **body):
        status, job = self.call("POST", "/api/items/%s/publish" % item, body, tenant=tenant)
        self.assertEqual(status, 200, job)
        for _ in range(200):
            status, job = self.call("GET", "/api/jobs/" + job["id"], tenant=tenant)
            if job["state"] != "running":
                return job
            time.sleep(0.05)
        self.fail("job did not finish")

    def test_00_proxy_key_and_tenant_header_required(self):
        self.assertEqual(self.call("GET", "/api/health", headers={"X-VCSP-Proxy-Key": ""})[0], 200)
        status, res = self.call("GET", "/api/config", headers={"X-VCSP-Proxy-Key": "wrong"})
        self.assertEqual(status, 403)
        self.assertIn("web server", res["error"])
        self.assertEqual(self.call("GET", "/api/config", tenant="")[0], 404)
        self.assertEqual(self.call("GET", "/api/config", tenant="nosuchtenant")[0], 404)
        self.assertEqual(self.call("GET", "/api/config", tenant="../etc")[0], 404)

    def test_01_csrf_and_names(self):
        self.assertEqual(self.call("POST", "/api/uploads", {"item": "a"}, headers={"X-VCSP-Request": ""})[0], 403)
        self.assertEqual(self.call("POST", "/api/uploads", {"item": "a", "filename": "a.iso", "size": 1},
                                   headers={"Origin": "https://evil.example"})[0], 403)
        status, res = self.call("POST", "/api/uploads", {"item": "ok", "filename": "run.exe", "size": 10})
        self.assertEqual(status, 400)
        self.assertIn("not allowed", res["error"])
        self.assertEqual(self.call("POST", "/api/uploads", {"item": "../etc", "filename": "a.iso", "size": 10})[0], 400)
        self.assertEqual(self.call("POST", "/api/uploads", {"item": "x", "filename": "../a.iso", "size": 10})[0], 400)

    def test_02_template_upload_resume_publish(self):
        files = template("ubuntu")
        disk = files["ubuntu-disk1.vmdk"]
        # start the disk, then send a chunk at the wrong offset: server tells us where to resume
        status, init = self.call("POST", "/api/uploads", {"item": "ubuntu", "filename": "ubuntu-disk1.vmdk",
                                                          "size": len(disk)})
        self.call("PUT", "/api/uploads/" + init["id"], raw=disk[:MB], headers={
            "Content-Range": "bytes 0-%d/%d" % (MB - 1, len(disk))})
        status, res = self.call("PUT", "/api/uploads/" + init["id"], raw=disk[:MB], headers={
            "Content-Range": "bytes 0-%d/%d" % (MB - 1, len(disk))})
        self.assertEqual((status, res["offset"]), (409, MB))
        for name, data in files.items():
            self.assertEqual(self.upload("ubuntu", name, data)[0], 200)
        job = self.publish("ubuntu", description="Ubuntu 22.04 golden image")
        self.assertEqual(job["state"], "done", job)
        items = {i["name"]: i for i in self.env.lib_json("items.json")["items"]}
        self.assertEqual(items["ubuntu"]["type"], "vcsp.ovf")
        self.assertEqual(items["ubuntu"]["description"], "Ubuntu 22.04 golden image")
        with open(os.path.join(self.env.lib, "ubuntu", "ubuntu-disk1.vmdk"), "rb") as fh:
            self.assertEqual(fh.read(), disk)

    def test_03_wrong_content_rejected_on_first_chunk(self):
        status, res = self.upload("fake", "disk.vmdk", b"MZ" + os.urandom(3 * MB))
        self.assertEqual(status, 422)
        self.assertIn("not a VMDK", res["error"])
        status, res = self.upload("fake", "win.iso", os.urandom(40000))
        self.assertEqual(status, 422)

    def test_04_manifest_mismatch_blocks_publish(self):
        files = template("bad")
        lines = files["bad.mf"].decode().splitlines()
        files["bad.mf"] = "".join((l.split("= ")[0] + "= " + "0" * 64 if l.startswith("SHA256(bad-disk1") else l) + "\n"
                                  for l in lines).encode()
        for name, data in files.items():
            self.assertEqual(self.upload("bad", name, data)[0], 200)
        job = self.publish("bad")
        self.assertEqual(job["state"], "failed")
        self.assertIn("Checksum verification failed", job["error"])
        self.assertFalse(os.path.exists(os.path.join(self.env.lib, "bad")))

    def test_05_ova_is_extracted(self):
        files = template("appliance")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
            for name in ("appliance.ovf", "appliance.mf", "appliance-disk1.vmdk", "appliance.nvram"):
                info = tarfile.TarInfo(name)
                info.size = len(files[name])
                tar.addfile(info, io.BytesIO(files[name]))
        self.assertEqual(self.upload("appliance", "appliance.ova", buf.getvalue())[0], 200)
        job = self.publish("appliance")
        self.assertEqual(job["state"], "done", job)
        self.assertEqual(sorted(os.listdir(os.path.join(self.env.lib, "appliance"))),
                         ["appliance-disk1.vmdk", "appliance.mf", "appliance.nvram", "appliance.ovf", "item.json"])

    def test_06_malicious_ova_refused(self):
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
            payload = b"<Envelope/>"
            info = tarfile.TarInfo("../../escape.ovf")
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        self.assertEqual(self.upload("evil", "evil.ova", buf.getvalue())[0], 200)
        job = self.publish("evil")
        self.assertEqual(job["state"], "failed")
        self.assertIn("unsafe name", job["error"])
        self.assertFalse(os.path.exists(os.path.join(self.env.tmp, "data", "escape.ovf")))

    def test_07_media_merge_and_delete(self):
        self.assertEqual(self.upload("media", "win2022.iso", iso_image())[0], 200)
        self.assertEqual(self.publish("media")["state"], "done")
        self.assertEqual(self.upload("media", "rhel9.iso", iso_image(80000))[0], 200)
        job = self.publish("media", mode="merge")
        self.assertEqual(job["state"], "done", job)
        names = {i["name"] for i in self.env.lib_json("items.json")["items"]}
        self.assertTrue({"win2022", "rhel9"} <= names, names)
        version = int(self.env.lib_json()["version"])
        status, job = self.call("DELETE", "/api/items/media")
        self.assertEqual(status, 200)
        for _ in range(100):
            job = self.call("GET", "/api/jobs/" + job["id"])[1]
            if job["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(job["state"], "done", job)
        self.assertGreater(int(self.env.lib_json()["version"]), version)
        names = {i["name"] for i in self.env.lib_json("items.json")["items"]}
        self.assertFalse({"win2022", "rhel9"} & names)

    def test_09_tenant_isolation(self):
        self.env.add_tenant("acme", "ACME Bank")
        self.env.add_tenant("globex")
        status, cfg = self.call("GET", "/api/config", tenant="acme")
        self.assertEqual((cfg["tenant"], cfg["lib_name"]), ("acme", "ACME Bank"))
        self.assertEqual(cfg["subscription_url"], "https://vcsp.test.local/tenants/acme/lib/lib.json")
        self.assertEqual(self.upload("acme-media", "acme.iso", iso_image(), tenant="acme")[0], 200)
        job = self.publish("acme-media", tenant="acme")
        self.assertEqual(job["state"], "done", job)
        # published into acme's own library with its own lib.json
        acme_items = {i["name"] for i in self.env.lib_json("items.json", tenant="acme")["items"]}
        self.assertEqual(acme_items, {"acme-media"})
        self.assertNotEqual(self.env.lib_json(tenant="acme")["id"], self.env.lib_json()["id"])
        # invisible to the provider and to other tenants
        provider_items = {i["name"] for i in self.call("GET", "/api/items")[1]["items"]}
        self.assertNotIn("acme-media", provider_items)
        self.assertEqual(self.call("GET", "/api/items", tenant="globex")[1]["items"], [])
        # another tenant cannot read, publish or delete it, or see acme's jobs
        self.assertEqual(self.call("GET", "/api/jobs/" + job["id"], tenant="globex")[0], 404)
        self.assertEqual(self.call("GET", "/api/jobs/" + job["id"])[0], 404)
        self.assertEqual(self.call("DELETE", "/api/items/acme-media", tenant="globex")[0], 404)
        # staged uploads are per tenant too
        self.assertEqual(self.upload("shared-name", "x.iso", iso_image(), tenant="globex")[0], 200)
        self.assertEqual(self.call("GET", "/api/uploads", tenant="acme")[1]["ready"], [])
        self.assertEqual(self.call("DELETE", "/api/staged/shared-name", tenant="globex")[0], 200)

    def test_10_tenant_quota(self):
        self.env.add_tenant("tiny", quota_gb=0.0001)          # about 107 KB
        status, cfg = self.call("GET", "/api/config", tenant="tiny")
        self.assertEqual(cfg["quota_bytes"], int(0.0001 * (1 << 30)))
        status, res = self.upload("big", "big.iso", iso_image(200 * 1024), tenant="tiny")
        self.assertEqual(status, 507)
        self.assertIn("quota", res["error"])
        self.assertEqual(self.upload("small", "small.iso", iso_image(64 * 1024), tenant="tiny")[0], 200)

    def test_11_suspended_tenant(self):
        self.env.add_tenant("paused", state="suspended")
        status, res = self.call("GET", "/api/config", tenant="paused")
        self.assertEqual(status, 403)
        self.assertIn("suspended", res["error"])

    def test_12_index_all_skips_suspended(self):
        self.env.add_tenant("idx-a")
        self.env.add_tenant("idx-off", state="suspended")
        scopes = self.env.index("--all")["scopes"]
        self.assertIn("provider", scopes)
        self.assertIn("idx-a", scopes)
        self.assertNotIn("idx-off", scopes)
        single = self.env.index("--tenant", "idx-a")
        self.assertEqual(single["tenant"], "idx-a")
        bad = subprocess.run([sys.executable, INDEX, "--config", self.env.conf, "--tenant", "nope"], capture_output=True)
        self.assertEqual(bad.returncode, 3)

    def test_08_listing(self):
        status, cfg = self.call("GET", "/api/config")
        self.assertEqual(cfg["subscription_url"], "https://vcsp.test.local/lib/lib.json")
        self.assertEqual(cfg["user"], "tester")
        status, items = self.call("GET", "/api/items")
        self.assertEqual(status, 200)
        by_name = {i["name"]: i for i in items["items"]}
        self.assertEqual(by_name["ubuntu"]["status"], "published")


if __name__ == "__main__":
    unittest.main()

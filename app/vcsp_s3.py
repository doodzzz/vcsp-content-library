# -*- coding: utf-8 -*-
"""
vcsp_s3 - minimal S3 client for the upload portal (Python standard library only).

Lists a bucket and downloads one object from Amazon S3 or S3-compatible object
storage (on-premises stores included), signing every request with AWS
Signature Version 4. Downloads stream to disk in 1 MiB blocks, resume with
HTTP Range requests after a dropped connection, and are checked against the
object's MD5 ETag whenever S3 exposes one.

Security properties the portal relies on:
  * the caller supplies an endpoint that the portal already matched against
    the provider's allow-list (S3_ALLOWED_ENDPOINTS), so tenants cannot make
    the server reach arbitrary hosts;
  * redirects are never followed; bucket names are validated before they
    become part of a host name (virtual-hosted addressing);
  * TLS certificates are always verified (system trust store, plus S3_CA_FILE
    for a private CA);
  * credentials live only in this object, for one listing or one download.
"""

import hashlib
import hmac
import http.client
import re
import socket
import ssl
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
REGION_RE = re.compile(r"^[a-z0-9-]{1,32}$")
ENDPOINT_RE = re.compile(r"^https?://[A-Za-z0-9.-]+(:\d{1,5})?$")
BLOCK = 1 << 20
MAX_LIST_BYTES = 16 << 20

FRIENDLY = {
    "InvalidAccessKeyId": "The access key is not valid for this S3 endpoint.",
    "SignatureDoesNotMatch": "The secret key does not match the access key.",
    "AccessDenied": "Access denied: these credentials may not read this bucket or object.",
    "NoSuchBucket": "The bucket does not exist on this endpoint.",
    "NoSuchKey": "The object does not exist (it may have been moved or deleted).",
    "ExpiredToken": "The session token has expired.",
    "InvalidToken": "The session token is not valid.",
}


class S3Error(Exception):
    def __init__(self, message, status=None, code=None):
        super().__init__(message)
        self.status, self.code = status, code


class S3Cancelled(Exception):
    pass


def uri_encode(value, keep_slash):
    return quote(value, safe="/-_.~" if keep_slash else "-_.~")


def sign_v4(method, host, path, query, headers, access_key, secret_key, region,
            payload_hash=EMPTY_SHA256, now=None, session_token=None):
    """AWS Signature Version 4 for S3. `path` must already be URI-encoded (it is used as-is,
    because S3 paths are never normalized). Returns (headers with Authorization, canonical request)."""
    now = now or datetime.now(timezone.utc)
    amz_date, day = now.strftime("%Y%m%dT%H%M%SZ"), now.strftime("%Y%m%d")
    hdrs = {k.lower(): " ".join(str(v).split()) for k, v in headers.items()}
    hdrs.update({"host": host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date})
    if session_token:
        hdrs["x-amz-security-token"] = session_token
    names = sorted(hdrs)
    canonical_headers = "".join("%s:%s\n" % (n, hdrs[n]) for n in names)
    signed = ";".join(names)
    canonical_query = "&".join("%s=%s" % (uri_encode(k, False), uri_encode(v, False))
                               for k, v in sorted(query.items()))
    canonical = "\n".join([method, path, canonical_query, canonical_headers, signed, payload_hash])
    scope = "%s/%s/s3/aws4_request" % (day, region)
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = ("AWS4" + secret_key).encode()
    for part in (day, region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    hdrs["authorization"] = "AWS4-HMAC-SHA256 Credential=%s/%s,SignedHeaders=%s,Signature=%s" % (
        access_key, scope, signed, signature)
    return hdrs, canonical


def _md5():
    try:
        return hashlib.md5(usedforsecurity=False)      # integrity check only; works in FIPS mode
    except TypeError:
        return hashlib.md5()


class S3Client:
    def __init__(self, endpoint, region, bucket, access_key, secret_key, session_token=None,
                 addressing="path", ca_file=None, timeout=60):
        endpoint = endpoint.rstrip("/")
        if not ENDPOINT_RE.match(endpoint):
            raise S3Error("The S3 endpoint must look like https://s3.example.local[:port].")
        if not BUCKET_RE.match(bucket or "") or ".." in bucket:
            raise S3Error("Bucket names use 3-63 lowercase letters, digits, dots and hyphens.")
        if not REGION_RE.match(region or ""):
            raise S3Error("The region must look like us-east-1 or me-south-1.")
        if not access_key or not secret_key:
            raise S3Error("Enter the access key ID and the secret access key.")
        if addressing not in ("path", "virtual"):
            raise S3Error("Addressing must be path or virtual.")
        parts = urlsplit(endpoint)
        self.https = parts.scheme == "https"
        self.endpoint, self.region, self.bucket = endpoint, region, bucket
        self.access_key, self.secret_key, self.session_token = access_key, secret_key, session_token or None
        self.timeout = timeout
        host, port = parts.hostname, parts.port
        if addressing == "virtual":
            host = "%s.%s" % (bucket, host)
            self.base_path = ""
        else:
            self.base_path = "/" + bucket
        self.connect_host, self.port = host, port or (443 if self.https else 80)
        default_port = (self.https and self.port == 443) or (not self.https and self.port == 80)
        self.host_header = host if default_port else "%s:%d" % (host, self.port)
        self.context = None
        if self.https:
            self.context = ssl.create_default_context()
            if ca_file:
                self.context.load_verify_locations(cafile=ca_file)

    # ------------------------------------------------------------ transport
    def _connect(self):
        if self.https:
            return http.client.HTTPSConnection(self.connect_host, self.port, timeout=self.timeout, context=self.context)
        return http.client.HTTPConnection(self.connect_host, self.port, timeout=self.timeout)

    def _request(self, method, path, query=None, extra=None):
        query = query or {}
        hdrs, _ = sign_v4(method, self.host_header, path, query, extra or {}, self.access_key,
                          self.secret_key, self.region, session_token=self.session_token)
        url = path + ("?" + "&".join("%s=%s" % (uri_encode(k, False), uri_encode(v, False))
                                     for k, v in sorted(query.items())) if query else "")
        send = {("Host" if k == "host" else k): v for k, v in hdrs.items()}
        conn = self._connect()
        try:
            conn.request(method, url, headers=send)
            return conn, conn.getresponse()
        except ssl.SSLCertVerificationError as exc:
            conn.close()
            raise S3Error("The TLS certificate of %s is not trusted (%s). The provider can add the issuing CA "
                          "with S3_CA_FILE." % (self.connect_host, exc.verify_message))
        except socket.gaierror:
            conn.close()
            raise S3Error("Cannot resolve %s; check DNS on the library server." % self.connect_host)
        except (ConnectionRefusedError, TimeoutError, socket.timeout, OSError) as exc:
            conn.close()
            raise S3Error("Cannot connect to %s:%d (%s)." % (self.connect_host, self.port, exc.__class__.__name__))

    def _error(self, resp):
        body = resp.read(65536)
        code = message = ""
        try:
            import xml.etree.ElementTree as ET
            root = ET.fromstring(body)
            code = root.findtext("Code") or ""
            message = root.findtext("Message") or ""
            region = root.findtext("Region") or root.findtext("Endpoint") or ""
        except Exception:
            region = ""
        if resp.status in (301, 307) or code in ("PermanentRedirect", "AuthorizationHeaderMalformed"):
            hint = resp.getheader("x-amz-bucket-region") or region
            return S3Error("The bucket is served from another region or address%s; adjust the region or addressing "
                           "style. (%s)" % (" (%s)" % hint if hint else "", message or code or resp.status),
                           resp.status, code)
        text = FRIENDLY.get(code) or message or "S3 answered HTTP %d." % resp.status
        return S3Error("%s%s" % (text, " [%s]" % code if code else ""), resp.status, code)

    def _object_path(self, key):
        return "%s/%s" % (self.base_path, uri_encode(key, True))

    # ------------------------------------------------------------ operations
    def list(self, prefix="", continuation=None, max_keys=1000):
        """One page of ListObjectsV2: {"objects": [...], "next": token or None}."""
        import xml.etree.ElementTree as ET
        query = {"list-type": "2", "max-keys": str(max_keys)}
        if prefix:
            query["prefix"] = prefix
        if continuation:
            query["continuation-token"] = continuation
        conn, resp = self._request("GET", self.base_path or "/", query)
        try:
            if resp.status != 200:
                raise self._error(resp)
            body = resp.read(MAX_LIST_BYTES + 1)
        finally:
            conn.close()
        if len(body) > MAX_LIST_BYTES:
            raise S3Error("The listing is unexpectedly large; narrow it with a prefix.")
        root = ET.fromstring(body)
        ns = root.tag[:root.tag.index("}") + 1] if root.tag.startswith("{") else ""
        objects = []
        for c in root.iter(ns + "Contents"):
            objects.append({"key": c.findtext(ns + "Key") or "", "size": int(c.findtext(ns + "Size") or 0),
                            "modified": c.findtext(ns + "LastModified") or "",
                            "etag": (c.findtext(ns + "ETag") or "").strip('"')})
        truncated = (root.findtext(ns + "IsTruncated") or "").lower() == "true"
        return {"objects": objects, "next": root.findtext(ns + "NextContinuationToken") if truncated else None}

    def head(self, key):
        conn, resp = self._request("HEAD", self._object_path(key))
        try:
            resp.read()
            if resp.status != 200:
                if resp.status == 404:
                    raise S3Error(FRIENDLY["NoSuchKey"], 404, "NoSuchKey")
                if resp.status == 403:
                    raise S3Error(FRIENDLY["AccessDenied"], 403, "AccessDenied")
                raise S3Error("S3 answered HTTP %d for %s." % (resp.status, key), resp.status)
            return {"size": int(resp.getheader("Content-Length") or 0),
                    "etag": (resp.getheader("ETag") or "").strip('"'),
                    "sse": resp.getheader("x-amz-server-side-encryption")}
        finally:
            conn.close()

    def download(self, key, dest, size, progress=None, cancelled=None, retries=5):
        """Stream the object to `dest`; resume after network drops. Returns integrity details."""
        offset, attempt, md5, meta = 0, 0, _md5(), {}
        with open(dest, "wb") as out:
            while offset < size or size == 0:
                extra = {"Range": "bytes=%d-" % offset} if offset else {}
                try:
                    conn, resp = self._request("GET", self._object_path(key), extra=extra)
                except S3Error as exc:
                    # a network error while resuming is retried; a first connection failure is reported at once
                    if exc.status is not None or offset == 0 or attempt >= retries:
                        raise
                    attempt += 1
                    time.sleep(min(30, 2 ** attempt))
                    continue
                try:
                    if resp.status not in (200, 206):
                        raise self._error(resp)
                    if offset and resp.status == 200:          # range ignored: start over
                        out.seek(0)
                        out.truncate()
                        offset, md5 = 0, _md5()
                    meta = {"etag": (resp.getheader("ETag") or "").strip('"'),
                            "sse": resp.getheader("x-amz-server-side-encryption")}
                    while True:
                        if cancelled and cancelled():
                            raise S3Cancelled()
                        block = resp.read(BLOCK)
                        if not block:
                            break
                        out.write(block)
                        md5.update(block)
                        offset += len(block)
                        if progress:
                            progress(offset, size)
                    if size == 0:
                        break
                except (http.client.HTTPException, ConnectionError, TimeoutError, socket.timeout, ssl.SSLError) as exc:
                    attempt += 1
                    if attempt > retries:
                        raise S3Error("The download kept failing after %d retries (%s)." % (retries, exc.__class__.__name__))
                    time.sleep(min(30, 2 ** attempt))
                    continue
                finally:
                    conn.close()
                if offset < size:                              # connection closed early: resume
                    attempt += 1
                    if attempt > retries:
                        raise S3Error("The download kept stopping early after %d retries." % retries)
                    continue
            out.flush()
        if offset != size:
            raise S3Error("Received %d bytes, but the object is %d bytes." % (offset, size))
        etag, sse = meta.get("etag", ""), meta.get("sse")
        if re.fullmatch(r"[0-9a-f]{32}", etag or "") and sse in (None, "AES256"):
            if md5.hexdigest() != etag:
                raise S3Error("The downloaded data does not match the object's MD5 checksum (ETag).")
            return {"verified": "md5", "etag": etag}
        return {"verified": None, "etag": etag}

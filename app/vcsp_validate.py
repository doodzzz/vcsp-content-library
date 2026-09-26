# -*- coding: utf-8 -*-
"""
vcsp_validate - content checks for the VCSP upload portal (Python stdlib only).

Everything a user uploads passes three gates before it reaches the library:
  1. name gate     - item and file names match a strict pattern and the file
                     extension is on the ALLOWED_EXTENSIONS allow-list;
  2. content gate  - the bytes match the extension (VMDK sparse header, ISO
                     9660/UDF volume descriptor, OVA tar magic, OVF XML, ...),
                     checked on the first chunk and again when complete;
  3. package gate  - the item as a whole is deployable: one OVF descriptor,
                     every referenced disk present with the declared size,
                     manifest checksums correct, OVAs safely unpacked.
"""

import hashlib
import os
import re
import shutil
import tarfile
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import unquote

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")
FILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
MF_LINE = re.compile(r"^(SHA1|SHA256|SHA512|MD5)\s*\((.+)\)\s*=\s*([0-9A-Fa-f]+)\s*$")
RESERVED_NAMES = {"item.json", "items.json", "lib.json"}
OVF_PARTS = {"vmdk", "mf", "cert", "nvram"}
SMALL_TEXT_LIMIT = 1 << 20          # .mf and .cert
NVRAM_LIMIT = 64 << 20
OVF_LIMIT = 64 << 20
COPY_BUF = 1 << 20

# bytes needed before the header check can decide
SNIFF_BYTES = {"iso": 32774, "ova": 262, "vmdk": 64, "ovf": 64}


class ValidationError(Exception):
    """A user-facing reason why content was refused."""


def ext_of(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def check_item_name(item):
    if not isinstance(item, str) or not NAME_RE.match(item) or item.endswith("."):
        raise ValidationError(
            "Item names use 1-80 letters, digits, dots, hyphens or underscores and start with a "
            "letter or digit (for example ubuntu-22.04-template).")
    return item


def check_file_name(filename, allowed):
    if not isinstance(filename, str) or not FILE_RE.match(filename) or filename.lower() in RESERVED_NAMES:
        raise ValidationError(
            "%r is not a valid file name. Use letters, digits, dots, hyphens or underscores, "
            "starting with a letter or digit." % filename)
    ext = ext_of(filename)
    if ext not in allowed:
        raise ValidationError("%s is not allowed. Allowed file types: %s."
                              % (filename, ", ".join("." + e for e in allowed)))
    return ext


def new_hash(algo):
    try:
        return hashlib.new(algo, usedforsecurity=False)   # works under OpenSSL FIPS mode
    except TypeError:
        return hashlib.new(algo)


def hash_file(path, algo, bufsize=8 << 20):
    h = new_hash(algo)
    buf = bytearray(bufsize)
    view = memoryview(buf)
    with open(path, "rb", buffering=0) as fh:
        try:
            os.posix_fadvise(fh.fileno(), 0, 0, os.POSIX_FADV_SEQUENTIAL)
        except (AttributeError, OSError):
            pass
        while True:
            n = fh.readinto(buf)
            if not n:
                break
            h.update(view[:n])
    return h.hexdigest()


# ---------------------------------------------------------------- content gate
def sniff(path, ext, size, complete):
    """Return None if the bytes look like `ext`, else a short reason.

    Header checks run as soon as enough bytes are on disk, so a mislabelled
    40 GB file is refused after its first chunk instead of at the end.
    """
    if not complete and size < SNIFF_BYTES.get(ext, 1 << 62):
        return None
    with open(path, "rb") as fh:
        head = fh.read(65536)
        if ext == "vmdk":
            if head[:4] in (b"KDMV", b"COWD") or head.lstrip().startswith(b"# Disk DescriptorFile"):
                return None
            return "it is not a VMDK disk (no sparse or streamOptimized header; flat extents cannot be used in OVF)"
        if ext == "ova":
            return None if head[257:262] == b"ustar" else "it is not an OVA (tar) archive"
        if ext == "iso":
            fh.seek(32769)
            return None if fh.read(5) in (b"CD001", b"BEA01") else "it is not an ISO 9660 or UDF image"
        if ext == "ovf":
            text = head.lstrip(b"\xef\xbb\xbf \t\r\n")
            if not text.startswith(b"<"):
                return "it is not an XML OVF descriptor"
            if complete:
                if size > OVF_LIMIT:
                    return "the descriptor is larger than 64 MiB"
                info = ovf_references(path)
                if info["error"]:
                    return "the descriptor is not valid OVF XML (%s)" % info["error"]
            return None
        if not complete:
            return None
        if ext == "mf":
            if size > SMALL_TEXT_LIMIT:
                return "a manifest larger than 1 MiB is not plausible"
            fh.seek(0)
            for line in fh.read().decode("utf-8", "replace").splitlines():
                if line.strip() and not MF_LINE.match(line.strip()):
                    return "line %r is not a manifest entry like SHA256(disk.vmdk)= <hex>" % line[:60]
            return None
        if ext == "cert":
            if size > SMALL_TEXT_LIMIT:
                return "a certificate file larger than 1 MiB is not plausible"
            fh.seek(0)
            return None if b"-----BEGIN CERTIFICATE-----" in fh.read() else "it contains no PEM certificate"
        if ext == "nvram":
            return None if size <= NVRAM_LIMIT else "an NVRAM file larger than 64 MiB is not plausible"
    return None


# ---------------------------------------------------------------- OVF / OVA
def ovf_references(path):
    """{"refs": {href: {"size": int|None, "chunked": bool}}, "vapp": bool, "error": str|None}"""
    try:
        import xml.etree.ElementTree as ET
    except ImportError:
        return {"refs": {}, "vapp": False, "error": "xml.etree unavailable (install python3-xml)"}
    refs, vapp = {}, False
    try:
        root_seen = False
        for _event, elem in ET.iterparse(path, events=("start",)):
            tag = elem.tag.rpartition("}")[2]
            if not root_seen:
                root_seen = True
                if tag != "Envelope":
                    return {"refs": {}, "vapp": False, "error": "root element is <%s>, expected <Envelope>" % tag}
            if tag == "File":
                attrs = {k.rpartition("}")[2]: v for k, v in elem.attrib.items()}
                if attrs.get("href"):
                    size = attrs.get("size", "")
                    refs[attrs["href"]] = {"size": int(size) if size.isdigit() else None,
                                           "chunked": "chunkSize" in attrs}
            elif tag == "VirtualSystemCollection":
                vapp = True
    except Exception as exc:
        return {"refs": {}, "vapp": False, "error": str(exc)}
    return {"refs": refs, "vapp": vapp, "error": None}


def extract_ova(ova_path, dest_dir, allowed):
    """Unpack an OVA in one streaming pass. Returns the extracted file names.

    Only flat, regular files with allowed extensions are accepted: absolute
    paths, '..', sub-folders, links and device nodes are refused, so a crafted
    archive cannot write outside the item's staging folder.
    """
    label = os.path.basename(ova_path)
    names = []
    try:
        with tarfile.open(ova_path, mode="r|") as tar:
            for member in tar:
                name = member.name
                while name.startswith("./"):
                    name = name[2:]
                if member.isdir() and name in ("", "."):
                    continue
                if not member.isfile():
                    raise ValidationError("%s: %r is not a regular file; links, devices and folders "
                                          "are not accepted inside an OVA" % (label, member.name))
                if "/" in name or not FILE_RE.match(name) or name.lower() in RESERVED_NAMES:
                    raise ValidationError("%s: entry %r has an unsafe name" % (label, member.name))
                ext = ext_of(name)
                if ext not in allowed or ext == "ova":
                    raise ValidationError("%s: %s is not an allowed file type" % (label, name))
                if name in names:
                    raise ValidationError("%s: %s appears twice in the archive" % (label, name))
                src = tar.extractfile(member)
                tmp = os.path.join(dest_dir, ".%s.extract" % name)
                with open(tmp, "wb") as out:
                    shutil.copyfileobj(src, out, COPY_BUF)
                os.replace(tmp, os.path.join(dest_dir, name))
                names.append(name)
    except tarfile.TarError as exc:
        raise ValidationError("%s is not a readable OVA archive (%s)" % (label, exc))
    if not any(ext_of(n) == "ovf" for n in names):
        raise ValidationError("%s contains no .ovf descriptor" % label)
    for n in names:
        reason = sniff(os.path.join(dest_dir, n), ext_of(n), os.path.getsize(os.path.join(dest_dir, n)), True)
        if reason:
            raise ValidationError("%s: %s was rejected because %s" % (label, n, reason))
    return names


# ---------------------------------------------------------------- package gate
def validate_package(files):
    """files: {name: path}. Returns (kind, notes); raises ValidationError when not publishable."""
    by_ext = {}
    for name in files:
        by_ext.setdefault(ext_of(name), []).append(name)
    ovfs, isos = sorted(by_ext.get("ovf", [])), sorted(by_ext.get("iso", []))
    notes = []
    if len(ovfs) > 1:
        raise ValidationError("An item holds one OVF template; found %s. Upload each template as its own item."
                              % ", ".join(ovfs))
    if ovfs and isos:
        raise ValidationError("OVF templates and ISO media must be separate items (found %s with %s)."
                              % (ovfs[0], ", ".join(isos)))
    if not ovfs:
        stray = sorted(n for n in files if ext_of(n) in OVF_PARTS)
        if stray:
            raise ValidationError("%s belong to an OVF template; add the .ovf descriptor to this item."
                                  % ", ".join(stray))
        if isos:
            if len(isos) > 1:
                notes.append("Each ISO is published as its own media item: %s." % ", ".join(isos))
            return "media", notes
        if by_ext.get("ova"):
            notes.append("OVA files are published as plain files; subscribers cannot deploy them as templates.")
        return "other", notes

    info = ovf_references(files[ovfs[0]])
    if info["error"]:
        raise ValidationError("%s is not valid OVF: %s" % (ovfs[0], info["error"]))
    problems, referenced = [], set()
    for ref, meta in sorted(info["refs"].items()):
        if "://" in ref:
            continue
        name = unquote(ref)
        referenced.add(name)
        if meta["chunked"]:
            referenced.update(n for n in files if n.startswith(name + "."))
            if name + ".000000000" not in files:
                problems.append("%s.000000000 (chunked file) is missing" % name)
            continue
        if name not in files:
            problems.append("%s is missing" % name)
        elif meta["size"] is not None and os.path.getsize(files[name]) != meta["size"]:
            problems.append("%s is %d bytes but the OVF declares %d"
                            % (name, os.path.getsize(files[name]), meta["size"]))
    if problems:
        raise ValidationError("The template is incomplete: " + "; ".join(problems) + ".")
    extra = sorted(n for n in files if n not in referenced and ext_of(n) not in ("ovf", "mf", "cert"))
    if extra:
        notes.append("Not referenced by %s and published as extra files: %s." % (ovfs[0], ", ".join(extra)))
    return ("vApp template" if info["vapp"] else "VM template"), notes


def verify_manifest(mf_path, resolve, workers=4):
    """Check every digest listed in an OVF manifest. Returns a list of problems (empty = OK)."""
    entries, problems = [], []
    with open(mf_path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            m = MF_LINE.match(line)
            if not m:
                problems.append("unreadable manifest line %r" % line[:60])
                continue
            entries.append((m.group(1).lower(), m.group(2).strip(), m.group(3).lower()))
    tasks = []
    for algo, name, expected in entries:
        path = resolve(name)
        if path is None:
            problems.append("%s is listed in the manifest but was not uploaded" % name)
        else:
            tasks.append((algo, name, expected, path))
    if tasks:
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(tasks)))) as pool:
            digests = list(pool.map(lambda t: hash_file(t[3], t[0]), tasks))
        for (algo, name, expected, _path), actual in zip(tasks, digests):
            if actual != expected:
                problems.append("%s %s mismatch (manifest %s..., file %s...)"
                                % (name, algo.upper(), expected[:12], actual[:12]))
    return problems

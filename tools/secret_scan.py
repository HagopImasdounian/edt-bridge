#!/usr/bin/env python3
"""Scan a checkout for committed secrets and files that must never be tracked.

Usage: python3 tools/secret_scan.py [path]      (default: current directory)

Scans tracked files (or every file, outside a git checkout). Hits print as
path:line: rule, and the matched secret text is never printed. Exit 1 on any hit.
Stdlib only.
"""
import fnmatch
import os
import re
import subprocess
import sys

DATA_URI = re.compile(r"data:[\w/+.\-]+;base64,[A-Za-z0-9+/=]{200,}")
MAX_LINE = 20000

SECRET_RULES = [
    ("bridge API key", re.compile(r"edt_[a-f0-9]{12}_[A-Za-z0-9_-]{43}"), None),
    ("PEM private key header", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY"), None),
    ("hard-coded secret assignment",
     re.compile(r"(?i)(?:api[_-]?key|secret|token|password)\s*[:=]\s*[\"']([^\"'\s]{16,})[\"']"), 1),
    ("AWS access key id", re.compile(r"AKIA[0-9A-Z]{16}"), None),
    ("GitHub token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"), None),
    ("DigitalOcean token", re.compile(r"dop_v1_[a-f0-9]{64}"), None),
    ("64-hex value assigned to aud",
     re.compile(r"(?i)\baud\w*[\"']?\s*[:=]\s*[\"']?[a-f0-9]{64}\b"), None),
]

PLACEHOLDER_WORDS = ("example", "placeholder", "changeme", "change-me", "your",
                     "xxxx", "....", "<", ">", "redacted", "dummy", "fake", "sample")

FORBIDDEN_NAMES = [".env*", "*.db", "*.pem", "*.key", "*ItemList.json", "*CustomerList.json"]
FORBIDDEN_DIRS = {"customers", "private", "plugins"}
SKIP_DIRS = {".git", "__pycache__", "node_modules"}


def is_placeholder(value):
    v = value.lower()
    if any(w in v for w in PLACEHOLDER_WORDS):
        return True
    if re.fullmatch(r"[A-Z][A-Z0-9_]*", value):          # env-var name
        return True
    if value.startswith(("$", "%", "{{", "os.environ", "process.env")):
        return True
    return len(set(value)) <= 2


def forbidden_file(path):
    parts = path.replace("\\", "/").split("/")
    base = parts[-1]
    for d in parts[:-1]:
        if d.lower() in FORBIDDEN_DIRS:
            return "forbidden file: under %s/" % d
    if base == "config.example.json":
        return None
    if base == "config.json" or fnmatch.fnmatch(base, "config.*.json"):
        return "forbidden file: config file"
    for pat in FORBIDDEN_NAMES:
        if fnmatch.fnmatch(base.lower(), pat.lower()):
            return "forbidden file: matches %s" % pat
    return None


def list_files(root):
    try:
        out = subprocess.run(["git", "-C", root, "ls-files", "-z"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, check=True).stdout
        names = [n for n in out.decode("utf-8", "replace").split("\0") if n]
        if names:
            return names
    except (OSError, subprocess.CalledProcessError):
        pass
    names = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
        for f in files:
            names.append(os.path.relpath(os.path.join(d, f), root))
    return names


def main(argv):
    root = argv[1] if len(argv) > 1 else "."
    hits = []
    for name in list_files(root):
        why = forbidden_file(name)
        if why:
            hits.append((name, 0, why))
        fp = os.path.join(root, name)
        if not os.path.isfile(fp) or os.path.islink(fp):
            continue
        with open(fp, "rb") as f:
            data = f.read()
        if b"\0" in data[:8000]:
            continue
        for i, line in enumerate(data.decode("utf-8", "replace").split("\n"), 1):
            line = DATA_URI.sub("data:", line[:MAX_LINE])
            for rule, rx, grp in SECRET_RULES:
                for m in rx.finditer(line):
                    if grp and is_placeholder(m.group(grp)):
                        continue
                    hits.append((name, i, "secret: " + rule))
                    break
    for path, line, rule in hits:
        print("%s:%d: %s" % (path, line, rule))
    if hits:
        print("secret_scan: %d hit(s)" % len(hits), file=sys.stderr)
        return 1
    print("secret_scan: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

"""
Check a built image for secrets and local state before it is published (CI runs it
before pushing to GHCR, so the package can be public).

    python scripts/scan_image.py coderag:ci        # the image: its files, ENV and build history
    python scripts/scan_image.py --dir .           # the same file checks on a directory tree

Fails (exit 1) on:
  - files that hold secrets or local state by name: .env files and SQLite databases (outside
    the OS's own /usr, /var, /etc, /lib, /bin); anywhere, SSH keys, .git-credentials, .netrc,
    .pypirc and Azure/GitHub/Docker CLI config; a .git directory in /app;
  - secret-shaped strings (Groq, GitHub, OpenAI/OpenRouter, Google, Hugging Face and AWS keys,
    private keys, database URLs with a password) in the app's own files: /app, /opt/demos,
    /data, /home, /root, /tmp. Third-party code (/opt/venv, /usr, the model and grammars) is
    only checked by name;
  - ENV values or build-history lines that look like secrets.
Findings name the path and the kind of secret, never the value. Standard library only.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tarfile

PATTERNS = {
    "Groq key": r"gsk_[A-Za-z0-9]{20,}",
    "GitHub token": r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,})",
    "OpenAI/OpenRouter key": r"\bsk-[A-Za-z0-9_-]{20,}",
    "Google API key": r"\bAIza[0-9A-Za-z_-]{35}",
    "Hugging Face token": r"\bhf_[A-Za-z0-9]{30,}",
    "AWS access key": r"\bAKIA[0-9A-Z]{16}\b",
    "private key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "database URL with a password": r"\b(?:postgres|postgresql|mysql)(?:\+\w+)?://[^\s:/@'\"<>]+:[^\s@'\"<>{}]+@",
}
SECRET_RE = re.compile("|".join(f"(?P<g{i}>{p})" for i, p in enumerate(PATTERNS.values())))
KINDS = list(PATTERNS)

CONTENT_ROOTS = ("app/", "opt/demos/", "data/", "home/", "root/", "tmp/")
SKIP_ROOTS = ("proc/", "sys/", "dev/")
SYSTEM_ROOTS = ("/usr/", "/var/", "/etc/", "/lib", "/bin/", "/sbin/")
SECRET_NAMES = {".git-credentials", ".netrc", ".pypirc", "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"}
SECRET_DIRS = ("/.azure/", "/.config/gh/", "/.docker/", "/.ssh/")
SECRET_ENV_NAME = re.compile(r"SECRET|TOKEN|PASSWORD|PASSWD|API_KEY|PRIVATE_KEY|DATABASE_URL|TOKEN_KEY", re.I)
MAX_BYTES = 8 * 1024 * 1024


def kind_of(match):
    return KINDS[int(match.lastgroup[1:])]


def name_problem(path):
    """Why this path must not be in the image (by its name alone), or None."""
    p = "/" + path.lstrip("./").replace("\\", "/")
    base = p.rsplit("/", 1)[-1]
    ours = not p.startswith(SYSTEM_ROOTS)            # the OS's own files may use these names
    if ours and (base == ".env" or base.endswith(".env")):
        return "an .env file"
    if ours and re.search(r"\.(db|sqlite|sqlite3)(-wal|-shm|-journal)?$", base):
        return "a database file"
    if base in SECRET_NAMES:
        return "a credentials file"
    if any(d in p for d in SECRET_DIRS):
        return "CLI credentials"
    if p.startswith("/app/.git/") or p == "/app/.git":
        return "a .git directory"
    return None


def content_problems(data):
    """The kinds of secret-shaped strings in `data` (bytes), sorted, without the values."""
    if len(data) > MAX_BYTES or b"\0" in data[:4096]:
        return []
    return sorted({kind_of(m) for m in SECRET_RE.finditer(data.decode("utf-8", "ignore"))})


def in_content_scope(path):
    p = path.lstrip("./").replace("\\", "/")
    return p.startswith(CONTENT_ROOTS)


def scan_files(entries):
    """entries: iterable of (path relative to the root, read() -> bytes). Returns (findings, files)."""
    findings, files = [], 0
    for path, read in entries:
        p = path.lstrip("./").replace("\\", "/")
        if p.startswith(SKIP_ROOTS):
            continue
        files += 1
        problem = name_problem(p)
        if problem == "a .git directory":
            p = "app/.git"                              # once for the whole directory
        if problem and f"/{p}: {problem}" not in findings:
            findings.append(f"/{p}: {problem}")
        if in_content_scope(p):
            for kind in content_problems(read()):
                findings.append(f"/{p}: {kind}")
    return findings, files


def scan_config(env, history):
    """ENV lines ("NAME=value") and build-history lines of the image."""
    findings = []
    for line in env or []:
        name, _, value = line.partition("=")
        if SECRET_ENV_NAME.search(name) and value:
            findings.append(f"ENV {name}: has a value")
        for kind in content_problems(value.encode()):
            findings.append(f"ENV {name}: {kind}")
    for i, line in enumerate(history or []):
        for kind in content_problems(line.encode()):
            findings.append(f"build history step {i}: {kind}")
    return findings


def dir_entries(root):
    for base, dirs, names in os.walk(root):
        for d in dirs:                                  # report directories such as .git by name too
            if name_problem(os.path.relpath(os.path.join(base, d), root)):
                yield os.path.relpath(os.path.join(base, d), root), lambda: b""
        for n in names:
            full = os.path.join(base, n)
            if os.path.isfile(full) and not os.path.islink(full):
                yield os.path.relpath(full, root), (lambda f=full: open(f, "rb").read())


def image_entries(image):
    """Every regular file of the image's filesystem (docker create + docker export, streamed)."""
    cid = subprocess.run(["docker", "create", image], check=True, capture_output=True, text=True).stdout.strip()
    try:
        proc = subprocess.Popen(["docker", "export", cid], stdout=subprocess.PIPE)
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            for member in tar:
                if member.isdir() and name_problem(member.name):
                    yield member.name, lambda: b""
                elif member.isfile():
                    f = tar.extractfile(member)
                    data = f.read() if member.size <= MAX_BYTES else b""
                    yield member.name, (lambda d=data: d)
        proc.wait()
    finally:
        subprocess.run(["docker", "rm", cid], capture_output=True)


def image_config(image):
    env = json.loads(subprocess.run(["docker", "image", "inspect", image, "--format", "{{json .Config.Env}}"],
                                    check=True, capture_output=True, text=True).stdout or "null")
    history = subprocess.run(["docker", "history", "--no-trunc", "--format", "{{.CreatedBy}}", image],
                             check=True, capture_output=True, text=True).stdout.splitlines()
    return env, history


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("image", nargs="?", help="a local Docker image, e.g. coderag:ci")
    parser.add_argument("--dir", help="scan this directory instead of an image")
    args = parser.parse_args(argv)
    if bool(args.image) == bool(args.dir):
        parser.error("give an image or --dir")
    if args.dir:
        findings, files = scan_files(dir_entries(args.dir))
        target = args.dir
    else:
        findings, files = scan_files(image_entries(args.image))
        findings += scan_config(*image_config(args.image))
        target = args.image
    for f in findings:
        print("FAIL", f)
    print(f"{'FAILED' if findings else 'OK'}: {len(findings)} finding(s) in {files} files of {target}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())

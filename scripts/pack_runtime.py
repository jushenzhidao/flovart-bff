import os, zipfile, datetime, subprocess

SRC = r"D:\code\flovart-bff"
OUT_DIR = r"D:\code\flovart-bff\dist"
ver = "0.1.0"
try:
    ver = subprocess.check_output(
        ["git", "-C", SRC, "describe", "--tags", "--always"],
        text=True, stderr=subprocess.DEVNULL,
    ).strip() or ver
except Exception:
    pass
stamp = datetime.datetime.now().strftime("%Y%m%d")
name = f"flovart-bff-{ver}-{stamp}.zip"
os.makedirs(OUT_DIR, exist_ok=True)
out = os.path.join(OUT_DIR, name)

TOP_FILES = [
    "requirements.txt",
    "pyproject.toml",
    ".env.example",
    ".gitignore",
    "README.md",
    "ARCHITECTURE.md",
    "DESIGN-cloud-persistence.md",
    "IMAGE-ASYNC-TASKS-CONTRACT.md",
    "MATERIAL_SHARING_DESIGN.md",
    "THIRD-PARTY-IMAGE-API-REFERENCE.md",
]
TOP_DIRS = ["app", "docs"]

EXCLUDE_DIRS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".git", ".workbuddy",
                ".e2e", "data", "dist", "node_modules", ".venv"}
EXCLUDE_FILE_SUFFIX = (".pyc", ".pyo", ".pyd")
EXCLUDE_FILES = {".env", "flovart_cloud.db", "_bff.log"}

added, skipped = [], []
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for f in TOP_FILES:
        p = os.path.join(SRC, f)
        if os.path.isfile(p):
            z.write(p, f); added.append(f)
        else:
            skipped.append(f)
    for d in TOP_DIRS:
        base = os.path.join(SRC, d)
        if not os.path.isdir(base):
            skipped.append(d + "/"); continue
        for root, dirs, files in os.walk(base):
            dirs[:] = [x for x in dirs if x not in EXCLUDE_DIRS and not x.startswith(".")]
            for fn in files:
                if fn in EXCLUDE_FILES or fn.endswith(EXCLUDE_FILE_SUFFIX):
                    continue
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, SRC).replace("\\", "/")
                z.write(full, rel); added.append(rel)

print(f"输出: {out}")
print(f"大小: {os.path.getsize(out)/1024:.1f} KB")
print(f"条目数: {len(added)}")
print("--- 顶层文件 ---")
for a in sorted(added):
    if "/" not in a:
        print("  " + a)
if skipped:
    print("--- 缺失(跳过) ---")
    for s in skipped:
        print("  " + s)

#!/usr/bin/env python3
"""Fold М9 Recorder (5 lessons) into М9 EdgeVMS as Lessons 5–9, and rewrite
every reference course-wide. Run from the course root. Idempotent-ish: refuses
if М9_EdgeVMS/05-* already exists."""
import os, re, shutil, sys

ROOT = os.getcwd()
OLD, NEW = "М9_EdgeVMS", "М9_EdgeVMS"
SHIFT = 4
TEXT_EXT = (".md", ".py", ".go", ".sh", ".hcl", ".mod", ".txt", ".sql", ".container", ".service", ".conf", ".toml", ".yaml", ".yml")

if any(f.startswith("05-") for f in os.listdir(NEW)):
    sys.exit("М9 already has a lesson 5; refusing")

old_lessons = sorted(f for f in os.listdir(OLD) if re.match(r"0[1-5]-.*\.md$", f))
assert len(old_lessons) == 5, old_lessons
rename = {f: f"{int(f[:2]) + SHIFT:02d}-{f[3:]}" for f in old_lessons}

def text_files():
    for d, dirs, files in os.walk(ROOT):
        dirs[:] = [x for x in dirs if x not in (".git", "__pycache__", "node_modules", "domain-controller-v1")]
        for f in files:
            if f.endswith(TEXT_EXT) or f == "go.mod":
                yield os.path.join(d, f)

def shift_bare(s):
    """Bare 'Lesson N' / 'Lessons A–B' (no module prefix) inside a moved file: +SHIFT."""
    def one(m):
        return f"{m.group(1)}{int(m.group(2)) + SHIFT}"
    s = re.sub(r"(?<!М\d )(?<!М\d\d )(\bLesson )([1-5])\b(?!\d)", one, s)
    def rng(m):
        return f"{m.group(1)}{int(m.group(2)) + SHIFT}{m.group(3)}{int(m.group(4)) + SHIFT}"
    s = re.sub(r"(?<!М\d )(?<!М\d\d )(\bLessons )([1-5])([–-])([1-5])\b(?!\d)", rng, s)
    return s

moved_prefix = os.path.join(ROOT, OLD)
changed = 0
for path in list(text_files()):
    try:
        s = open(path, encoding="utf-8").read()
    except UnicodeDecodeError:
        continue
    o = s
    in_moved = path.startswith(moved_prefix)
    in_m9 = path.startswith(os.path.join(ROOT, NEW))
    # 1. module-qualified lesson refs
    s = re.sub(r"М9 Lesson ([1-5])\b(?!\d)", lambda m: f"М9 Lesson {int(m.group(1)) + SHIFT}", s)
    s = re.sub(r"М9 Lessons ([1-5])([–-])([1-5])\b(?!\d)",
               lambda m: f"М9 Lessons {int(m.group(1)) + SHIFT}{m.group(2)}{int(m.group(3)) + SHIFT}", s)
    # 2. bare refs inside the moved files
    if in_moved:
        s = shift_bare(s)
        s = s.replace("**Module:** Recorder — one recorder learns what it should be (Module 9)",
                      "**Module:** EdgeVMS — the box owns its truth (Module 9)")
        s = s.replace("(Module 9)", "(Module 9)")
    # 3. lesson file names in links
    for a, b in rename.items():
        s = s.replace(a, b)
    # 4. paths
    s = s.replace(f"{OLD}/module-design.md", f"{NEW}/recorder-design.md")
    if in_moved:
        # its OWN design record first (before М9's collapses onto the same name)
        s = s.replace("(module-design.md)", "(recorder-design.md)").replace("(./module-design.md)", "(recorder-design.md)")
        s = s.replace("(../module-design.md)", "(../recorder-design.md)").replace("(../../module-design.md)", "(../../recorder-design.md)")
        s = s.replace(f"../{NEW}/", "").replace(f"./{NEW}/", "")     # now the same directory
        s = s.replace(f"../{OLD}/", "").replace(f"./{OLD}/", "")
    elif in_m9:
        s = s.replace(f"../{OLD}/", "./").replace(f"./{OLD}/", "./")
    s = s.replace(OLD, NEW)
    # 5. the bare module name
    s = re.sub(r"М9(?!\d)", "М9", s)
    s = s.replace("Module 9", "Module 9")
    if s != o:
        open(path, "w", encoding="utf-8").write(s); changed += 1
print("rewrote", changed, "files")

# 6. moves
for a, b in rename.items():
    shutil.move(os.path.join(OLD, a), os.path.join(NEW, b))
shutil.move(os.path.join(OLD, "module-design.md"), os.path.join(NEW, "recorder-design.md"))
for d in ("recorder", "recorder-go", "reference"):
    if os.path.isdir(os.path.join(OLD, d)):
        shutil.move(os.path.join(OLD, d), os.path.join(NEW, d))
old_readme = open(os.path.join(OLD, "README.md"), encoding="utf-8").read()
os.remove(os.path.join(OLD, "README.md"))
open(os.path.join(NEW, "_node-readme.md"), "w", encoding="utf-8").write(old_readme)   # merged by hand next
print("moved; old README parked as", os.path.join(NEW, "_node-readme.md"))
print("left in", OLD, ":", os.listdir(OLD))

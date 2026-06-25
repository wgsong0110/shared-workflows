#!/usr/bin/env python3
"""conda-diff.py — Package-level diff between base and project conda envs.

Compares conda-meta package manifests. Packages with identical
name+version+build are skipped. New/changed packages' files go into a
diff tarball. Removed packages' files go into a delete list.

Usage:
    python conda-diff.py BASE_ENV PROJECT_ENV OUTPUT_DIR

Produces:
    OUTPUT_DIR/conda-diff.tar.gz    — files to overlay on base env
    OUTPUT_DIR/conda-diff-delete.txt — files to remove from base env (may be empty)
    OUTPUT_DIR/conda-diff-meta.json  — diff metadata (package counts, sizes)
"""
import csv
import glob
import json
import os
import subprocess
import sys


def get_conda_packages(env_dir):
    """Return {name==version==build: [relative_file_paths]} from conda-meta."""
    pkgs = {}
    meta_dir = os.path.join(env_dir, "conda-meta")
    if not os.path.isdir(meta_dir):
        return pkgs
    for meta_path in glob.glob(os.path.join(meta_dir, "*.json")):
        fname = os.path.basename(meta_path)
        if fname in ("history",):
            continue
        try:
            with open(meta_path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        name = data.get("name", "")
        version = data.get("version", "")
        build = data.get("build", "")
        key = f"{name}=={version}=={build}"
        files = data.get("files", [])
        files.append(os.path.relpath(meta_path, env_dir))
        pkgs[key] = files
    return pkgs


def get_pip_packages(env_dir):
    """Return {pip:name==version: [relative_file_paths]} from dist-info/RECORD."""
    pkgs = {}
    sp_dirs = glob.glob(os.path.join(env_dir, "lib", "python*", "site-packages"))
    if not sp_dirs:
        return pkgs
    sp_dir = sp_dirs[0]
    sp_rel = os.path.relpath(sp_dir, env_dir)
    for dist_info in glob.glob(os.path.join(sp_dir, "*.dist-info")):
        metadata_file = os.path.join(dist_info, "METADATA")
        name = version = ""
        if os.path.exists(metadata_file):
            with open(metadata_file) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("Name: "):
                        name = line.split(": ", 1)[1]
                    elif line.startswith("Version: "):
                        version = line.split(": ", 1)[1]
                    elif not line:
                        break
        if not name:
            continue
        key = f"pip:{name}=={version}"
        record_file = os.path.join(dist_info, "RECORD")
        files = []
        if os.path.exists(record_file):
            with open(record_file, newline="") as f:
                for row in csv.reader(f):
                    if row and row[0]:
                        rel = os.path.normpath(os.path.join(sp_rel, row[0]))
                        files.append(rel)
        pkgs[key] = files
    return pkgs


def get_all_packages(env_dir):
    """Merge conda and pip package info. Pip packages override conda for same name."""
    conda = get_conda_packages(env_dir)
    pip = get_pip_packages(env_dir)
    return {**conda, **pip}


def compute_diff(base_dir, proj_dir):
    """Return (diff_files, delete_files, meta)."""
    base_pkgs = get_all_packages(base_dir)
    proj_pkgs = get_all_packages(proj_dir)

    base_owned = set()
    for files in base_pkgs.values():
        base_owned.update(files)

    proj_owned = set()
    for files in proj_pkgs.values():
        proj_owned.update(files)

    shared_keys = set(base_pkgs) & set(proj_pkgs)
    skip_files = set()
    for key in shared_keys:
        skip_files.update(proj_pkgs[key])

    diff_files = sorted(proj_owned - skip_files)

    removed_pkg_files = set()
    for key in set(base_pkgs) - set(proj_pkgs):
        removed_pkg_files.update(base_pkgs[key])
    delete_files = sorted(removed_pkg_files - proj_owned)

    for extra in ("conda-meta/history", "bin/conda-unpack",
                   "Scripts/conda-unpack.bat"):
        p = os.path.join(proj_dir, extra)
        if os.path.exists(p) and extra not in diff_files:
            diff_files.append(extra)

    new_pkgs = sorted(set(proj_pkgs) - set(base_pkgs))
    changed_pkgs = []
    for key in shared_keys:
        if set(proj_pkgs[key]) != set(base_pkgs[key]):
            changed_pkgs.append(key)
    removed_pkgs = sorted(set(base_pkgs) - set(proj_pkgs))

    meta = {
        "base_packages": len(base_pkgs),
        "project_packages": len(proj_pkgs),
        "new_packages": new_pkgs,
        "removed_packages": removed_pkgs,
        "diff_files": len(diff_files),
        "delete_files": len(delete_files),
    }
    return diff_files, delete_files, meta


def main():
    if len(sys.argv) != 4:
        print(f"Usage: {sys.argv[0]} BASE_ENV PROJECT_ENV OUTPUT_DIR",
              file=sys.stderr)
        sys.exit(1)

    base_dir = os.path.abspath(sys.argv[1])
    proj_dir = os.path.abspath(sys.argv[2])
    out_dir = os.path.abspath(sys.argv[3])
    os.makedirs(out_dir, exist_ok=True)

    print(f"[conda-diff] base: {base_dir}")
    print(f"[conda-diff] project: {proj_dir}")

    diff_files, delete_files, meta = compute_diff(base_dir, proj_dir)

    print(f"[conda-diff] {meta['base_packages']} base pkgs, "
          f"{meta['project_packages']} project pkgs")
    print(f"[conda-diff] +{len(meta['new_packages'])} new, "
          f"-{len(meta['removed_packages'])} removed")
    print(f"[conda-diff] {meta['diff_files']} files to add/update, "
          f"{meta['delete_files']} files to delete")

    if meta["new_packages"]:
        print(f"[conda-diff] new: {', '.join(meta['new_packages'])}")
    if meta["removed_packages"]:
        print(f"[conda-diff] removed: {', '.join(meta['removed_packages'])}")

    tar_path = os.path.join(out_dir, "conda-diff.tar.gz")
    if diff_files:
        file_list = os.path.join(out_dir, "_diff_files.txt")
        with open(file_list, "w") as f:
            f.write("\n".join(diff_files))
        rc = subprocess.run(
            ["tar", "czf", tar_path, "-C", proj_dir, "-T", file_list],
            capture_output=True,
        )
        os.unlink(file_list)
        if rc.returncode != 0:
            print(f"[conda-diff] tar error: {rc.stderr.decode()}", file=sys.stderr)
            sys.exit(1)
        size = os.path.getsize(tar_path)
        print(f"[conda-diff] diff tarball: {size / 1024 / 1024:.1f} MB")
        meta["diff_size_bytes"] = size
    else:
        subprocess.run(["tar", "czf", tar_path, "-T", "/dev/null"],
                        capture_output=True)
        meta["diff_size_bytes"] = os.path.getsize(tar_path)
        print("[conda-diff] diff tarball: empty (project == base)")

    delete_path = os.path.join(out_dir, "conda-diff-delete.txt")
    with open(delete_path, "w") as f:
        f.write("\n".join(delete_files))

    meta_path = os.path.join(out_dir, "conda-diff-meta.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    print("[conda-diff] done")


if __name__ == "__main__":
    main()

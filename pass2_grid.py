#!/usr/bin/env python3
"""Second pass for multi-product grid pages: extract EVERY candidate layer
(not just the largest per group). Targets listed in pass2_targets.json.
Writes images to images/{cat}/ and metadata to meta2/{cat}/p{NN}.json.
"""
import json, os, re, subprocess, sys, time

from extract_catalog import (FOLDERS, list_folder, download, sanitize,
                             save_jpg, page_num, git_push)
from psd_tools import PSDImage


def extract_all_layers(psd_path, cat, page):
    psd = PSDImage.open(psd_path)
    meta = {"catalog": cat, "page": page, "canvas": [psd.width, psd.height],
            "layers": [], "texts": []}
    for ly in psd.descendants():
        if ly.kind == "type":
            try:
                txt = str(ly.text)
            except Exception:
                txt = ""
            meta["texts"].append({"layer": ly.name, "text": txt,
                                  "bbox": [ly.left, ly.top, ly.right, ly.bottom]})
    seen = 0
    for ly in psd.descendants():
        if ly.is_group() or ly.kind not in ("smartobject", "pixel"):
            continue
        if ly.width * ly.height < 250_000 or ly.width < 300:
            continue
        # skip near-full-canvas backgrounds
        if ly.width > psd.width * 0.9 and ly.height > psd.height * 0.9:
            continue
        seen += 1
        out = f"images/{cat}/p{page:02d}_g_{seen:02d}_{sanitize(ly.name)}.jpg"
        try:
            size = save_jpg(ly.composite(), out)
            meta["layers"].append({
                "layer": ly.name, "kind": ly.kind,
                "bbox": [ly.left, ly.top, ly.right, ly.bottom],
                "image": out, "out_size": size})
        except Exception as e:
            meta["layers"].append({"layer": ly.name, "error": str(e)})
    os.makedirs(f"meta2/{cat}", exist_ok=True)
    with open(f"meta2/{cat}/p{page:02d}.json", "w") as f:
        json.dump(meta, f, indent=1, ensure_ascii=False)
    return len(meta["layers"])


def main():
    targets = json.load(open("pass2_targets.json"))  # e.g. ["134/25","134/27"]
    todo = []
    for t in targets:
        cat, page = t.split("/")
        if not os.path.exists(f"meta2/{cat}/p{int(page):02d}.json"):
            todo.append((cat, int(page)))
    if not todo:
        print("pass2: nothing to do")
        return
    for cat, page in todo:
        fid = None
        for folder_id in FOLDERS[cat]:
            for file_id, name in list_folder(folder_id):
                if page_num(name) == page and name.lower().endswith(".psd"):
                    fid = file_id
        if not fid:
            print(f"pass2: {cat}/p{page} not found in Drive folders")
            continue
        psd_path = f"/tmp/p2_{cat}_{page}.psd"
        download(fid, psd_path)
        n = extract_all_layers(psd_path, cat, page)
        print(f"pass2 [{cat} p{page:02d}]: {n} layers extracted", flush=True)
        os.remove(psd_path)
    git_push("pass2 grid extraction")


if __name__ == "__main__":
    main()

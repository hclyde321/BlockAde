"""Explicit one-time download. Runtime never downloads or uploads course data."""
import concurrent.futures
import hashlib
import json
from pathlib import Path
import urllib.request
import argparse

REPO = "mlx-community/Qwen3-8B-4bit"
REVISION = "545dc4251c05440727734bcd94334791f6ab0192"
TARGET = Path(__file__).resolve().parents[1] / "models" / "medical-qwen3-8b"
PROFILES = {
    "8b": (REPO, REVISION, TARGET.name),
    "30b": ("mlx-community/Qwen3-30B-A3B-Instruct-2507-4bit",
            "e9675aa3ca5f900ccef55267914466d55ab325fa", "medical-qwen3-30b-a3b"),
    "35b": ("mlx-community/Qwen3.6-35B-A3B-4bit",
            "38740b847e4cb78f352aba30aa41c76e08e6eb46", "medical-qwen36-35b-a3b"),
}


def main(profile="8b"):
    repo, revision, directory = PROFILES[profile]
    target_dir = TARGET.parent / directory
    with urllib.request.urlopen(f"https://huggingface.co/api/models/{repo}/revision/{revision}?blobs=true", timeout=60) as response:
        info = json.load(response)
    if info.get("sha") != revision:
        raise RuntimeError("模型版本與固定版本不符。")
    target_dir.mkdir(parents=True, exist_ok=True)
    def fetch(item):
        name = item["rfilename"]
        if name.startswith('.') or '/' in name:
            return
        target = target_dir / name
        digest = item.get("lfs", {}).get("sha256")
        if target.is_file() and target.stat().st_size == item["size"]:
            with target.open("rb") as source:
                if not digest or hashlib.file_digest(source,"sha256").hexdigest() == digest:
                    print(f"已驗證 {name}", flush=True)
                    return
        temporary = target.with_name(name + ".partial")
        h = hashlib.sha256()
        count = 0
        next_report = 128 * 1024 * 1024
        with urllib.request.urlopen(f"https://huggingface.co/{repo}/resolve/{revision}/{name}", timeout=120) as response, temporary.open("wb") as output:
            while chunk := response.read(4 * 1024 * 1024):
                output.write(chunk); h.update(chunk); count += len(chunk)
                if count >= next_report:
                    print(f"{name}: {count / item['size']:.0%} ({count / 1e9:.2f} GB)", flush=True)
                    next_report += 128 * 1024 * 1024
        if count != item["size"] or (digest and h.hexdigest() != digest):
            raise RuntimeError(f"下載驗證失敗：{name}")
        temporary.replace(target)
        print(f"下載完成且已驗證 {name}", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        list(executor.map(fetch, info["siblings"]))
    (target_dir / "download-manifest.json").write_text(json.dumps(info, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=PROFILES, default="8b")
    main(parser.parse_args().profile)

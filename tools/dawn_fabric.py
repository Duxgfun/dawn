#!/usr/bin/env python3
"""Dawn Launcher CLI: Quản lý và khởi chạy Minecraft 1.21.4 Fabric kèm mods.
Hỗ trợ đầy đủ việc tải game, cài đặt Fabric loader và nạp mods từ thư mục mods/.

Sử dụng:
    python tools/dawn_fabric.py create MyWorld
    python tools/dawn_fabric.py list-mods MyWorld
    python tools/dawn_fabric.py run MyWorld --memory 6G
"""

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOME = Path(os.environ.get("DAWN_HOME", os.environ.get("HORIZON_HOME", ROOT / ".local" / "dawn")))
SHARED_MC = ROOT / ".local" / "minecraft"
VERSION_1214_URL = "https://piston-meta.mojang.com/v1/packages/64843a304d7747da779448f853323962233f39ff/1.21.4.json"
FABRIC_META = "https://meta.fabricmc.net/v2"


def get_java_path():
    """Tìm đường dẫn Java 21 trên hệ thống."""
    candidates = [
        os.environ.get("JAVA_HOME", "") + "/bin/java.exe",
        r"C:\Program Files\Microsoft\jdk-21.0.12.101-hotspot\bin\java.exe",
        os.path.expanduser(r"~\openjdk\jdk-21.0.12.1+1\bin\java.exe"),
        "java",
    ]
    for c in candidates:
        if c and (os.path.exists(c) or c == "java"):
            try:
                out = subprocess.check_output([c, "-version"], stderr=subprocess.STDOUT, text=True)
                if "21." in out:
                    return c
            except Exception:
                pass
    return "java"


def download_file(url, dest, sha1=None):
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and sha1:
        with open(dest, "rb") as f:
            if hashlib.sha1(f.read()).hexdigest() == sha1:
                return dest
    req = urllib.request.Request(url, headers={"User-Agent": "Dawn-Launcher/1.0"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read()
    if sha1 and hashlib.sha1(data).hexdigest() != sha1:
        raise ValueError(f"Checksum mismatch cho {url}")
    with open(dest, "wb") as f:
        f.write(data)
    return dest


def get_latest_fabric_loader():
    """Lấy phiên bản Fabric Loader ổn định mới nhất cho 1.21.4."""
    url = f"{FABRIC_META}/versions/loader/1.21.4"
    req = urllib.request.Request(url, headers={"User-Agent": "Dawn-Launcher/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    for item in data:
        loader = item.get("loader", {})
        if loader.get("stable", False):
            return loader["version"]
    return data[0]["loader"]["version"]


def library_key(name):
    """Tách key group:artifact (hoặc kèm classifier) để đối chiếu thư viện."""
    parts = name.split(":")
    if len(parts) >= 4:
        return f"{parts[0]}:{parts[1]}:{parts[3]}"
    elif len(parts) >= 2:
        return f"{parts[0]}:{parts[1]}"
    return name


def rule_allowed(rules):
    if not rules:
        return True
    allowed = False
    cur_os = "windows" if platform.system() == "Windows" else ("osx" if platform.system() == "Darwin" else "linux")
    for r in rules:
        action = r.get("action") == "allow"
        os_rule = r.get("os", {})
        name = os_rule.get("name")
        if name:
            if name == cur_os:
                allowed = action
        else:
            allowed = action
    return allowed


def download_assets(index_path):
    """Tải các asset thiết yếu (font, icons, resourcepacks, lang, textures) để tránh lỗi NoSuchFileException."""
    with open(index_path, "r", encoding="utf-8") as f:
        idx = json.load(f)

    to_download = []
    for name, obj in idx.get("objects", {}).items():
        h = obj["hash"]
        dest = SHARED_MC / "assets" / "objects" / h[:2] / h
        if not (dest.exists() and dest.stat().st_size == obj["size"]):
            # Ưu tiên các file thiết yếu (không phải sound)
            if not name.startswith("minecraft/sounds/"):
                to_download.append((h, dest, obj["size"]))

    if to_download:
        print(f"[-] Đang tải bổ sung {len(to_download)} asset thiết yếu (fonts, textures, icons)...")

        def fetch(item):
            h, dest, _ = item
            dest.parent.mkdir(parents=True, exist_ok=True)
            url = f"https://resources.download.minecraft.net/{h[:2]}/{h}"
            req = urllib.request.Request(url, headers={"User-Agent": "Dawn-Launcher/1.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                dest.write_bytes(r.read())

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(fetch, to_download))
        print("[✓] Đã tải xong toàn bộ asset thiết yếu.")


def prepare_fabric(loader_version):
    """Tải và chuẩn bị Fabric profile cùng thư viện."""
    print(f"[-] Chuẩn bị Fabric Loader v{loader_version} cho Minecraft 1.21.4...")
    url = f"{FABRIC_META}/versions/loader/1.21.4/{loader_version}/profile/json"
    req = urllib.request.Request(url, headers={"User-Agent": "Dawn-Launcher/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        profile = json.loads(resp.read())

    fabric_libs = []
    fabric_keys = set()
    lib_dir = SHARED_MC / "libraries"
    for lib in profile.get("libraries", []):
        name = lib["name"]
        fabric_keys.add(library_key(name))
        parts = name.split(":")
        group, artifact, ver = parts[0].replace(".", "/"), parts[1], parts[2]
        rel_path = f"{group}/{artifact}/{ver}/{artifact}-{ver}.jar"
        lib_url = lib.get("url", "https://maven.fabricmc.net/") + rel_path
        dest = lib_dir / rel_path
        download_file(lib_url, dest)
        fabric_libs.append(str(dest))

    main_class = profile.get("mainClass", "net.fabricmc.loader.impl.launch.knot.KnotClient")
    return profile, fabric_libs, fabric_keys, main_class


def prepare_vanilla(fabric_keys=None):
    """Tải và chuẩn bị Minecraft 1.21.4 từ Mojang (loại bỏ thư viện trùng lặp với Fabric)."""
    fabric_keys = fabric_keys or set()
    print("[-] Kiểm tra dữ liệu Minecraft 1.21.4 từ Mojang...")
    SHARED_MC.mkdir(parents=True, exist_ok=True)
    vjson_path = SHARED_MC / "1.21.4.json"
    download_file(VERSION_1214_URL, vjson_path)
    with open(vjson_path, "r", encoding="utf-8") as f:
        vdata = json.load(f)

    # Tải client.jar
    client_dl = vdata["downloads"]["client"]
    client_jar = SHARED_MC / "versions" / "1.21.4" / "1.21.4.jar"
    download_file(client_dl["url"], client_jar, client_dl["sha1"])

    # Tải libraries
    libs = []
    lib_dir = SHARED_MC / "libraries"
    for lib in vdata.get("libraries", []):
        lib_name = lib.get("name", "")
        if not rule_allowed(lib.get("rules")):
            continue

        # Trên Windows x64, bỏ qua các bản natives arm64 và x86
        if platform.system() == "Windows":
            if ":natives-windows-arm64" in lib_name or ":natives-windows-x86" in lib_name:
                continue

        # Nếu Fabric đã cung cấp thư viện cùng group:artifact (ví dụ ASM), ưu tiên Fabric
        if library_key(lib_name) in fabric_keys:
            continue

        artifact = lib.get("downloads", {}).get("artifact")
        if artifact:
            dest = lib_dir / artifact["path"]
            download_file(artifact["url"], dest, artifact.get("sha1"))
            libs.append(str(dest))

    # Tải asset index và các asset thiết yếu
    asset_index_info = vdata.get("assetIndex", {})
    if asset_index_info:
        asset_idx_path = SHARED_MC / "assets" / "indexes" / f"{asset_index_info['id']}.json"
        download_file(asset_index_info["url"], asset_idx_path, asset_index_info.get("sha1"))
        download_assets(asset_idx_path)

    return vdata, client_jar, libs


def get_offline_uuid(name):
    """Tạo offline UUID chuẩn theo thuật toán Java UUID.nameUUIDFromBytes."""
    md5 = hashlib.md5(f"OfflinePlayer:{name}".encode("utf-8")).digest()
    b = bytearray(md5)
    b[6] = (b[6] & 0x0F) | 0x30
    b[8] = (b[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(b)))


def cmd_create(args):
    """Tạo một instance 1.21.4 Fabric."""
    inst_dir = DEFAULT_HOME / "instances" / args.name
    inst_dir.mkdir(parents=True, exist_ok=True)
    mods_dir = inst_dir / "mods"
    mods_dir.mkdir(exist_ok=True)
    (inst_dir / "saves").mkdir(exist_ok=True)
    (inst_dir / "resourcepacks").mkdir(exist_ok=True)

    loader = args.loader_version or get_latest_fabric_loader()
    cfg = {
        "name": args.name,
        "minecraft": "1.21.4",
        "loader": "fabric",
        "loader_version": loader,
    }
    with open(inst_dir / "instance.json", "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

    print(f"[✓] Đã tạo thành công instance Dawn 1.21.4 Fabric: {args.name}")
    print(f"    Thư mục game: {inst_dir}")
    print(f"    Thư mục mods: {mods_dir} (Thả các file .jar mods vào đây)")
    print(f"    Chạy game bằng: python tools/dawn_fabric.py run {args.name}")


def cmd_list_mods(args):
    """Liệt kê danh sách mods đã cài đặt."""
    inst_dir = DEFAULT_HOME / "instances" / args.name
    mods_dir = inst_dir / "mods"
    if not mods_dir.exists():
        print(f"[!] Không tìm thấy thư mục mods của instance {args.name}")
        return
    mods = list(mods_dir.glob("*.jar"))
    print(f"Danh sách mods trong {args.name} ({len(mods)} mods):")
    for m in mods:
        print(f"  - {m.name} ({m.stat().st_size // 1024} KB)")


def cmd_run(args):
    """Khởi chạy instance 1.21.4 Fabric kèm mods."""
    inst_dir = DEFAULT_HOME / "instances" / args.name
    cfg_file = inst_dir / "instance.json"
    if not cfg_file.exists():
        print(f"[!] Instance {args.name} chưa tồn tại. Đang tự động tạo...")
        cmd_create(args)

    with open(cfg_file, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    loader_version = cfg.get("loader_version") or get_latest_fabric_loader()
    profile, fabric_libs, fabric_keys, main_class = prepare_fabric(loader_version)
    vdata, client_jar, vanilla_libs = prepare_vanilla(fabric_keys)

    # Classpath: Thư viện Fabric lên trước, sau đó là thư viện vanilla đã deduplicate, cuối cùng là client.jar
    all_libs = fabric_libs + vanilla_libs + [str(client_jar)]
    sep = ";" if platform.system() == "Windows" else ":"
    classpath = sep.join(all_libs)

    # Natives dir
    natives_dir = SHARED_MC / "natives"
    natives_dir.mkdir(parents=True, exist_ok=True)

    java_bin = get_java_path()
    memory = args.memory or "6G"
    player_uuid = get_offline_uuid(args.username)

    # Extra JVM args từ Fabric
    extra_jvm = []
    for arg in profile.get("arguments", {}).get("jvm", []):
        if isinstance(arg, str):
            extra_jvm.append(arg.strip())

    cmd = [
        java_bin,
        f"-Xmx{memory}",
        "-XX:+UnlockExperimentalVMOptions",
        "-XX:+UseG1GC",
        "-XX:G1NewSizePercent=20",
        "-XX:G1ReservePercent=20",
        "-XX:MaxGCPauseMillis=50",
        "-XX:G1HeapRegionSize=32M",
        f"-Djava.library.path={natives_dir}",
        f"-Dorg.lwjgl.system.SharedLibraryExtractPath={natives_dir}",
    ] + extra_jvm + [
        "-cp",
        classpath,
        main_class,
        "--username",
        args.username,
        "--version",
        "1.21.4",
        "--gameDir",
        str(inst_dir),
        "--assetsDir",
        str(SHARED_MC / "assets"),
        "--assetIndex",
        "19",
        "--uuid",
        player_uuid,
        "--accessToken",
        "0",
    ]

    print(f"[+] Khởi chạy Dawn (Minecraft 1.21.4 Fabric) cho instance '{args.name}'...")
    print(f"    Java: {java_bin}")
    print(f"    Bộ nhớ (RAM): {memory}")
    print(f"    Thư mục mods: {inst_dir / 'mods'}")
    subprocess.run(cmd)


def main():
    parser = argparse.ArgumentParser(description="Dawn Launcher CLI - Minecraft 1.21.4 Fabric Runner")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="Tạo instance 1.21.4 Fabric mới")
    p_create.add_argument("name", help="Tên instance (vd: MyFabricWorld)")
    p_create.add_argument("--loader-version", help="Phiên bản Fabric Loader (mặc định: stable mới nhất)")
    p_create.set_defaults(func=cmd_create)

    p_list = sub.add_parser("list-mods", help="Liệt kê mods của instance")
    p_list.add_argument("name", help="Tên instance")
    p_list.set_defaults(func=cmd_list_mods)

    p_run = sub.add_parser("run", help="Chạy game 1.21.4 Fabric")
    p_run.add_argument("name", help="Tên instance")
    p_run.add_argument("--username", default="DawnPlayer", help="Tên người chơi")
    p_run.add_argument("--memory", default="6G", help="Dung lượng RAM (vd: 6G, 8G)")
    p_run.add_argument("--loader-version", help="Phiên bản Fabric Loader")
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

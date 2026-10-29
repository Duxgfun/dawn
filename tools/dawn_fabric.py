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
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HOME = Path(os.environ.get("DAWN_HOME", os.environ.get("HORIZON_HOME", ROOT / ".local" / "dawn")))
SHARED_MC = ROOT / ".local" / "minecraft"
VERSION_1214_URL = "https://piston-meta.mojang.com/v1/packages/64843a304d7747da779448f853323962233f39ff/1.21.4.json"
FABRIC_META = "https://meta.fabricmc.net/v2"

# Microsoft OAuth Client ID công khai (được chứng thực cho launcher Minecraft Java)
DEFAULT_MSA_CLIENT_ID = os.environ.get("DAWN_MSA_CLIENT_ID", os.environ.get("HORIZON_MSA_CLIENT_ID", "c36a9fb6-4f2a-41ff-90bd-ae7cc92031eb"))
ACCOUNTS_FILE = DEFAULT_HOME / "accounts.json"


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


def ensure_mcef_natives(inst_dir):
    """Tự động kiểm tra và tải thư viện Chromium/CEF cho MCEF mod nếu có."""
    mods_dir = inst_dir / "mods"
    if not mods_dir.exists():
        return None
    has_mcef = any("mcef" in p.name.lower() for p in mods_dir.glob("*.jar"))
    if not has_mcef:
        return None

    mcef_lib_dir = mods_dir / "mcef-libraries"
    mcef_native = mcef_lib_dir / "windows_amd64"
    if mcef_native.exists() and (mcef_native / "jcef.dll").exists():
        return mcef_native

    mcef_lib_dir.mkdir(parents=True, exist_ok=True)
    tar_path = mcef_lib_dir / "windows_amd64.tar.gz"
    url = "https://mcef-download.cinemamod.com/java-cef-builds/eaeb3d4370aa3526ee237ad1981ad59af3de4dd1/windows_amd64.tar.gz"
    print("[-] Đang tự động tải thư viện Chromium CEF cho MCEF mod (~118 MB)...")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req) as resp, open(tar_path, "wb") as f:
            while chunk := resp.read(1024 * 1024):
                f.write(chunk)
        import tarfile
        print("[-] Đang giải nén thư viện Chromium CEF...")
        with tarfile.open(tar_path, "r:gz") as tar:
            tar.extractall(mcef_lib_dir)
        print("[+] Đã cài đặt thư viện native Chromium CEF thành công!")
        return mcef_native
    except Exception as e:
        print(f"[!] Cảnh báo: Không thể tải CEF native tự động: {e}")
        return None


def load_accounts():
    """Đọc danh sách tài khoản đã lưu từ accounts.json."""
    if not ACCOUNTS_FILE.exists():
        return {"active": None, "accounts": {}}
    try:
        with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"active": None, "accounts": {}}


def save_accounts(data):
    """Lưu danh sách tài khoản vào accounts.json."""
    ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def http_request(url, method="GET", data=None, headers=None, is_json=False):
    """Thực hiện HTTP request tới API với xử lý mã lỗi và JSON."""
    h = {"User-Agent": "Dawn-Launcher/1.0"}
    if headers:
        h.update(headers)
    payload = None
    if data is not None:
        if is_json:
            payload = json.dumps(data).encode("utf-8")
            h["Content-Type"] = "application/json"
            h["Accept"] = "application/json"
        else:
            payload = urllib.parse.urlencode(data).encode("utf-8")
            h["Content-Type"] = "application/x-www-form-urlencoded"

    req = urllib.request.Request(url, data=payload, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8")
            return resp.status, json.loads(content) if content else {}
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            body = json.loads(raw)
        except Exception:
            body = {"error": raw}
        return e.code, body


def ms_exchange_tokens(ms_access_token):
    """Chuyển đổi Microsoft access token sang Xbox Live -> XSTS -> Minecraft token."""
    # 1. Xbox Live User Authenticate
    xbl_body = {
        "Properties": {
            "AuthMethod": "RPS",
            "SiteName": "user.auth.xboxlive.com",
            "RpsTicket": f"d={ms_access_token}",
        },
        "RelyingParty": "http://auth.xboxlive.com",
        "TokenType": "JWT",
    }
    status, xbl = http_request("https://user.auth.xboxlive.com/user/authenticate", method="POST", data=xbl_body, is_json=True)
    if status != 200 or "Token" not in xbl:
        raise ValueError(f"Xác thực Xbox Live thất bại: {xbl}")
    xbl_token = xbl["Token"]

    # 2. XSTS Authorize
    xsts_body = {
        "Properties": {
            "SandboxId": "RETAIL",
            "UserTokens": [xbl_token],
        },
        "RelyingParty": "rp://api.minecraftservices.com/",
        "TokenType": "JWT",
    }
    status, xsts = http_request("https://xsts.auth.xboxlive.com/xsts/authorize", method="POST", data=xsts_body, is_json=True)
    if "XErr" in xsts:
        code = xsts["XErr"]
        if code == 2148916233:
            raise ValueError("Tài khoản chưa có hồ sơ Xbox. Vui lòng đăng nhập một lần tại xbox.com.")
        elif code in (2148916236, 2148916237):
            raise ValueError("Tài khoản cần xác minh độ tuổi người lớn trên xbox.com.")
        elif code == 2148916238:
            raise ValueError("Tài khoản trẻ em (Child account) cần được phụ huynh thêm vào Microsoft Family.")
        else:
            raise ValueError(f"Xbox Security (XSTS) từ chối xác thực (Mã lỗi: {code}).")

    if status != 200 or "Token" not in xsts:
        raise ValueError(f"Xác thực XSTS thất bại: {xsts}")

    xsts_token = xsts["Token"]
    claims = xsts.get("DisplayClaims", {}).get("xui", [{}])[0]
    uhs = claims.get("uhs")
    xuid = claims.get("xid", "")
    if not uhs:
        raise ValueError("Không tìm thấy user hash (uhs) trong phản hồi XSTS.")

    # 3. Đăng nhập Minecraft Services
    mc_body = {"identityToken": f"XBL3.0 x={uhs};{xsts_token}"}
    status, mc = http_request("https://api.minecraftservices.com/authentication/login_with_xbox", method="POST", data=mc_body, is_json=True)
    if status != 200 or "access_token" not in mc:
        # Thử fallback qua launcher login
        launcher_body = {"xtoken": f"XBL3.0 x={uhs};{xsts_token}", "platform": "PC_LAUNCHER"}
        status, mc = http_request("https://api.minecraftservices.com/launcher/login", method="POST", data=launcher_body, is_json=True)
        if status != 200 or "access_token" not in mc:
            raise ValueError(f"Minecraft Services từ chối token: {mc}")

    mc_token = mc["access_token"]
    expires_in = mc.get("expires_in", 86400)
    mc_expires_at = int(time.time()) + int(expires_in)

    # 4. Kiểm tra hồ sơ Minecraft
    status, prof = http_request(
        "https://api.minecraftservices.com/minecraft/profile",
        headers={"Authorization": f"Bearer {mc_token}"}
    )
    if status == 404 or "id" not in prof:
        raise ValueError("Tài khoản Microsoft này chưa có hồ sơ Minecraft Java Edition (chưa tạo tên hoặc chưa mua game).")
    if status != 200:
        raise ValueError(f"Không thể đọc hồ sơ Minecraft: {prof}")

    return {
        "username": prof["name"],
        "uuid": prof["id"],
        "mc_token": mc_token,
        "mc_expires_at": mc_expires_at,
        "xuid": xuid,
    }


def refresh_account_if_needed(acc):
    """Làm mới token Minecraft nếu sắp hết hạn (< 30 phút)."""
    now = int(time.time())
    if now < acc.get("mc_expires_at", 0) - 1800:
        return acc

    print(f"[-] Đang làm mới token đăng nhập Microsoft cho {acc.get('username')}...")
    refresh_token = acc.get("ms_refresh_token")
    if not refresh_token:
        print("[!] Không tìm thấy refresh token. Vui lòng đăng nhập lại bằng: python tools/dawn_fabric.py login")
        return acc

    form = {
        "grant_type": "refresh_token",
        "client_id": DEFAULT_MSA_CLIENT_ID,
        "refresh_token": refresh_token,
        "scope": "XboxLive.signin offline_access",
    }
    status, res = http_request("https://login.microsoftonline.com/consumers/oauth2/v2.0/token", method="POST", data=form)
    if status != 200 or "access_token" not in res:
        print(f"[!] Không thể làm mới token Microsoft ({res.get('error_description', res.get('error'))}).")
        return acc

    new_ms_access = res["access_token"]
    new_ms_refresh = res.get("refresh_token", refresh_token)

    try:
        updated = ms_exchange_tokens(new_ms_access)
        updated["ms_refresh_token"] = new_ms_refresh
        acc.update(updated)

        data = load_accounts()
        if "accounts" in data and acc["uuid"] in data["accounts"]:
            data["accounts"][acc["uuid"]] = acc
            save_accounts(data)
        print(f"[+] Đã làm mới token thành công cho tài khoản {acc['username']}!")
    except Exception as e:
        print(f"[!] Lỗi khi cấp mới Minecraft token: {e}")

    return acc


def cmd_login(args):
    """Đăng nhập tài khoản Microsoft bằng Device Code Flow."""
    client_id = DEFAULT_MSA_CLIENT_ID
    print("=" * 70)
    print("  ĐĂNG NHẬP TÀI KHOẢN MICROSOFT (MINECRAFT PREMIUM / BẢN QUYỀN)")
    print("=" * 70)

    form = {
        "client_id": client_id,
        "scope": "XboxLive.signin offline_access",
    }
    status, d_res = http_request("https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode", method="POST", data=form)
    if status != 200 or "user_code" not in d_res:
        print(f"[!] Lỗi khi yêu cầu Device Code: {d_res}")
        return

    user_code = d_res["user_code"]
    verify_url = d_res.get("verification_uri", "https://www.microsoft.com/link")
    device_code = d_res["device_code"]
    interval = max(int(d_res.get("interval", 5)), 1)
    expires_in = int(d_res.get("expires_in", 900))
    deadline = time.time() + expires_in

    print(f"\n1. Mở trang đăng nhập: {verify_url}")
    print(f"2. Nhập mã code sau:   >>>  {user_code}  <<<")
    print(f"3. Đăng nhập tài khoản Microsoft sở hữu Minecraft: Java Edition.")
    print("=" * 70)

    try:
        webbrowser.open(verify_url)
    except Exception:
        pass

    print(f"Đang chờ bạn hoàn tất đăng nhập trên trình duyệt (hết hạn sau {expires_in // 60} phút)...")

    ms_access = None
    ms_refresh = None

    poll_form = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "client_id": client_id,
        "device_code": device_code,
    }

    while time.time() < deadline:
        time.sleep(interval)
        status, t_res = http_request("https://login.microsoftonline.com/consumers/oauth2/v2.0/token", method="POST", data=poll_form)
        if status == 200 and "access_token" in t_res:
            ms_access = t_res["access_token"]
            ms_refresh = t_res.get("refresh_token")
            break

        err = t_res.get("error")
        if err == "authorization_pending":
            continue
        elif err == "slow_down":
            interval += 5
            continue
        elif err == "expired_token":
            print("[!] Mã xác thực đã hết hạn. Vui lòng thử lại.")
            return
        elif err == "authorization_declined":
            print("[!] Bạn đã từ chối yêu cầu đăng nhập trên trình duyệt.")
            return
        else:
            print(f"[!] Lỗi đăng nhập: {t_res.get('error_description', err)}")
            return

    if not ms_access:
        print("[!] Hết thời gian chờ đăng nhập.")
        return

    print("\n[-] Đã nhận token Microsoft. Đang kết nối Xbox Live và Minecraft Services...")
    try:
        acc_info = ms_exchange_tokens(ms_access)
        acc_info["ms_refresh_token"] = ms_refresh

        data = load_accounts()
        uuid_str = acc_info["uuid"]
        data["accounts"][uuid_str] = acc_info
        data["active"] = uuid_str
        save_accounts(data)

        print("=" * 70)
        print(f"[+] ĐĂNG NHẬP THÀNH CÔNG!")
        print(f"    Tên nhân vật (IGN): {acc_info['username']}")
        print(f"    UUID:               {acc_info['uuid']}")
        print(f"    Tài khoản này đã được đặt làm tài khoản mặc định để chơi game.")
        print("=" * 70)
    except Exception as e:
        print(f"[!] Lỗi khi lấy thông tin Minecraft: {e}")


def cmd_accounts(args):
    """Liệt kê danh sách các tài khoản Microsoft đã lưu."""
    data = load_accounts()
    active_uuid = data.get("active")
    accounts = data.get("accounts", {})

    if not accounts:
        print("[-] Hiện chưa có tài khoản Microsoft nào được lưu.")
        print("    Chạy lệnh sau để đăng nhập: python tools/dawn_fabric.py login")
        return

    print(f"Danh sách tài khoản ({len(accounts)}):")
    for u, acc in accounts.items():
        tag = " [ĐANG CHỌN]" if u == active_uuid else ""
        print(f"  - {acc.get('username', 'Unknown')} (UUID: {u}){tag}")


def cmd_select(args):
    """Chọn tài khoản Microsoft làm mặc định."""
    data = load_accounts()
    accounts = data.get("accounts", {})
    target = args.username.lower()

    found = None
    for u, acc in accounts.items():
        if acc.get("username", "").lower() == target or u.lower() == target:
            found = u
            break

    if not found:
        print(f"[!] Không tìm thấy tài khoản '{args.username}' trong danh sách đã lưu.")
        return

    data["active"] = found
    save_accounts(data)
    print(f"[+] Đã chọn tài khoản: {accounts[found].get('username')} ({found})")


def cmd_logout(args):
    """Đăng xuất tài khoản Microsoft khỏi máy."""
    data = load_accounts()
    accounts = data.get("accounts", {})
    if not accounts:
        print("[-] Không có tài khoản nào được lưu.")
        return

    target = args.username
    if not target:
        target = data.get("active")
        if not target:
            print("[!] Vui lòng chỉ định tên tài khoản cần đăng xuất.")
            return

    target_l = target.lower()
    found = None
    for u, acc in accounts.items():
        if acc.get("username", "").lower() == target_l or u.lower() == target_l:
            found = u
            break

    if not found:
        print(f"[!] Không tìm thấy tài khoản '{target}'.")
        return

    name = accounts[found].get("username", found)
    del accounts[found]
    if data.get("active") == found:
        data["active"] = next(iter(accounts.keys())) if accounts else None

    save_accounts(data)
    print(f"[+] Đã đăng xuất tài khoản: {name}")


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

    # Natives dir & MCEF native libraries
    natives_dir = SHARED_MC / "natives"
    natives_dir.mkdir(parents=True, exist_ok=True)

    mcef_native = ensure_mcef_natives(inst_dir)
    mcef_lib_dir = inst_dir / "mods" / "mcef-libraries"

    java_bin = get_java_path()
    memory = args.memory or "6G"

    # Xác định tài khoản người chơi (Microsoft Account hoặc Offline)
    data = load_accounts()
    active_uuid = data.get("active")
    accounts = data.get("accounts", {})
    account = None

    if not getattr(args, "offline", False):
        if getattr(args, "account", None):
            target = args.account.lower()
            for u, a in accounts.items():
                if a.get("username", "").lower() == target or u.lower() == target:
                    account = a
                    break
        elif active_uuid and active_uuid in accounts:
            account = accounts[active_uuid]

    if account:
        account = refresh_account_if_needed(account)
        player_name = account["username"]
        player_uuid = account["uuid"]
        access_token = account["mc_token"]
        user_type = "msa"
        xuid = account.get("xuid", "")
        print(f"[+] Sử dụng tài khoản Microsoft (Premium): {player_name} (UUID: {player_uuid})")
    else:
        player_name = args.username
        player_uuid = get_offline_uuid(player_name)
        access_token = "0"
        user_type = "legacy"
        xuid = ""
        print(f"[-] Chơi ở chế độ Offline: {player_name} (UUID: {player_uuid})")
        print(f"    (Để dùng tài khoản Premium bản quyền, chạy: python tools/dawn_fabric.py login)")

    # Extra JVM args từ Fabric & MCEF
    extra_jvm = []
    for arg in profile.get("arguments", {}).get("jvm", []):
        if isinstance(arg, str):
            extra_jvm.append(arg.strip())

    if mcef_native and mcef_native.exists():
        java_lib_path = f"{natives_dir};{mcef_native}"
        extra_jvm.append(f"-Dmcef.libraries.path={mcef_lib_dir}")
        extra_jvm.append(f"-Djcef.path={mcef_native}")
    else:
        java_lib_path = str(natives_dir)

    cmd = [
        java_bin,
        f"-Xmx{memory}",
        "-XX:+UnlockExperimentalVMOptions",
        "-XX:+UseG1GC",
        "-XX:G1NewSizePercent=20",
        "-XX:G1ReservePercent=20",
        "-XX:MaxGCPauseMillis=50",
        "-XX:G1HeapRegionSize=32M",
        f"-Djava.library.path={java_lib_path}",
        f"-Dorg.lwjgl.system.SharedLibraryExtractPath={natives_dir}",
    ] + extra_jvm + [
        "-cp",
        classpath,
        main_class,
        "--username",
        player_name,
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
        access_token,
        "--userType",
        user_type,
        "--versionType",
        "release",
    ]

    if xuid:
        cmd.extend(["--xuid", xuid])

    print(f"[+] Khởi chạy Dawn (Minecraft 1.21.4 Fabric) cho instance '{args.name}'...")
    print(f"    Java: {java_bin}")
    print(f"    Bộ nhớ (RAM): {memory}")
    print(f"    Thư mục mods: {inst_dir / 'mods'}")

    env = dict(os.environ)
    if mcef_native and mcef_native.exists():
        env["PATH"] = f"{mcef_native};" + env.get("PATH", "")

    subprocess.run(cmd, env=env, cwd=inst_dir)


def main():
    parser = argparse.ArgumentParser(description="Dawn Launcher CLI - Minecraft 1.21.4 Fabric Runner")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # Quản lý tài khoản Microsoft
    p_login = sub.add_parser("login", help="Đăng nhập tài khoản Microsoft (Minecraft Premium)")
    p_login.set_defaults(func=cmd_login)

    p_accounts = sub.add_parser("accounts", help="Xem danh sách tài khoản Microsoft đã lưu")
    p_accounts.set_defaults(func=cmd_accounts)

    p_select = sub.add_parser("select", help="Chọn tài khoản Microsoft làm mặc định")
    p_select.add_argument("username", help="Tên người chơi (IGN) hoặc UUID")
    p_select.set_defaults(func=cmd_select)

    p_logout = sub.add_parser("logout", help="Đăng xuất tài khoản Microsoft")
    p_logout.add_argument("username", nargs="?", help="Tên tài khoản (để trống để đăng xuất tài khoản đang chọn)")
    p_logout.set_defaults(func=cmd_logout)

    p_create = sub.add_parser("create", help="Tạo instance 1.21.4 Fabric mới")
    p_create.add_argument("name", help="Tên instance (vd: MyFabricWorld)")
    p_create.add_argument("--loader-version", help="Phiên bản Fabric Loader (mặc định: stable mới nhất)")
    p_create.set_defaults(func=cmd_create)

    p_list = sub.add_parser("list-mods", help="Liệt kê mods của instance")
    p_list.add_argument("name", help="Tên instance")
    p_list.set_defaults(func=cmd_list_mods)

    p_run = sub.add_parser("run", help="Chạy game 1.21.4 Fabric")
    p_run.add_argument("name", help="Tên instance")
    p_run.add_argument("--username", default="DawnPlayer", help="Tên người chơi offline")
    p_run.add_argument("--memory", default="6G", help="Dung lượng RAM (vd: 6G, 8G)")
    p_run.add_argument("--loader-version", help="Phiên bản Fabric Loader")
    p_run.add_argument("--offline", action="store_true", help="Chơi ở chế độ Offline (bỏ qua tài khoản Microsoft)")
    p_run.add_argument("--account", help="Chỉ định tài khoản Microsoft muốn dùng")
    p_run.set_defaults(func=cmd_run)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

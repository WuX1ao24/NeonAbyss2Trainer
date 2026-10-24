# -*- coding: utf-8 -*-
"""
《霓虹深渊2》存档修改器 (Neon Abyss 2 Save Editor)
==================================================
功能：读取 / 修改 / 写回《霓虹深渊2》本地存档。

核心能力
--------
1. 自动定位游戏存档目录 SavesDir（游戏安装目录下）；
2. 解密存档（XXTEA + gzip，密钥已逆向确认）；
3. 解析 DLPN 容器，读取全部 37 个数据键；
4. 安全修改：保险库资源（金币/炸弹/钥匙/蛋）、霓虹币 Token、
   进化等级/经验、角色解锁、内容解锁、统计信息；
5. 写回前自动备份（原档保留为 *.bak_时间戳），写回后可再读回校验。

用法
----
  python neon_save_editor.py [--dir <游戏存档目录>] [--slot One|Two|Three]

若无参数，会自动在常见位置搜索游戏目录。
交互菜单见程序提示。全程中文，按数字键选择。

作者：Marvis File Agent
"""

import os
import sys
import json
import gzip
import struct
import shutil
import glob
import datetime

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# ============================== 常量 ==============================

# 存档加密密钥（已通过实测确认，来自 SaveKeyFallback K4_SteamTapTapPc）
SAVE_KEY = b"8G$!kL9@qP4*Zx3#rT"

# DLPN 容器魔数
DLPN_MAGIC = b"DLPN"

# 常见游戏安装路径（用于自动定位）
GAME_DIR_CANDIDATES = [
    r"D:\Steam\steamapps\common\Neon Abyss 2",
    r"C:\Program Files (x86)\Steam\steamapps\common\Neon Abyss 2",
    r"C:\Program Files\Steam\steamapps\common\Neon Abyss 2",
    r"E:\Steam\steamapps\common\Neon Abyss 2",
]

# ============================== XXTEA ==============================

def _to_uint32(data):
    n = (len(data) + 3) // 4
    v = [0] * n
    for i in range(n):
        idx = i * 4
        v[i] = int.from_bytes(data[idx:idx + 4].ljust(4, b"\x00"), "little") & 0xFFFFFFFF
    return v


def _mx(sumv, y, z, p, e, k):
    return (((z >> 5 ^ y << 2) + (y >> 3 ^ z << 4)) ^ ((sumv ^ y) + (k[(p & 3) ^ e] ^ z))) & 0xFFFFFFFF


def xxtea_encrypt(data, key):
    """标准 Unity XXTEA 变体（includeLength 模式）加密。"""
    kb = key[:16].ljust(16, b"\x00")
    k = [int.from_bytes(kb[i * 4:(i + 1) * 4], "little") for i in range(4)]
    length = len(data)
    n = (length + 3) // 4 + 1
    v = [0] * n
    for i in range(n - 1):
        idx = i * 4
        v[i] = int.from_bytes(data[idx:idx + 4].ljust(4, b"\x00"), "little") & 0xFFFFFFFF
    v[n - 1] = length
    delta = 0x9E3779B9
    q = 6 + 52 // n
    sumv = 0
    z = v[n - 1]
    y = v[0]
    for _ in range(q):
        sumv = (sumv + delta) & 0xFFFFFFFF
        e = (sumv >> 2) & 3
        for p in range(n - 1):
            y = v[p + 1]
            v[p] = (v[p] + _mx(sumv, y, z, p, e, k)) & 0xFFFFFFFF
            z = v[p]
        y = v[0]
        v[n - 1] = (v[n - 1] + _mx(sumv, y, z, n - 1, e, k)) & 0xFFFFFFFF
        z = v[n - 1]
    out = bytearray()
    for i in range(n):
        out += struct.pack("<I", v[i] & 0xFFFFFFFF)
    return bytes(out)


def xxtea_decrypt(data, key):
    """标准 Unity XXTEA 变体（includeLength 模式）解密。"""
    kb = key[:16].ljust(16, b"\x00")
    k = [int.from_bytes(kb[i * 4:(i + 1) * 4], "little") for i in range(4)]
    v = _to_uint32(data)
    n = len(v)
    if n < 2:
        return b""
    delta = 0x9E3779B9
    q = 6 + 52 // n
    y = v[0]
    z = v[n - 1]
    sumv = (q * delta) & 0xFFFFFFFF
    while sumv != 0:
        e = (sumv >> 2) & 3
        for p in range(n - 1, 0, -1):
            z = v[p - 1]
            v[p] = (v[p] - _mx(sumv, y, z, p, e, k)) & 0xFFFFFFFF
            y = v[p]
        z = v[n - 1]
        v[0] = (v[0] - _mx(sumv, y, z, 0, e, k)) & 0xFFFFFFFF
        y = v[0]
        sumv = (sumv - delta) & 0xFFFFFFFF
    ln = v[n - 1]
    out = bytearray()
    for i in range(n - 1):
        out += struct.pack("<I", v[i] & 0xFFFFFFFF)
    return bytes(out[:ln])


# ============================== DLPN 容器 ==============================

class SaveEntry:
    """一条 DLPN 数据条目。"""

    def __init__(self, key_bytes, type_id, value_bytes):
        self.key_bytes = key_bytes  # 原始字节（含结尾 \\x00）
        self.type_id = type_id
        self.value_bytes = value_bytes

    @property
    def key(self):
        return self.key_bytes.rstrip(b"\x00").decode("utf-8", "replace")

    def json_value(self):
        """若值为 JSON，返回解析后的对象；失败返回 None。"""
        try:
            return json.loads(self.value_bytes.decode("utf-8"))
        except Exception:
            return None

    def set_json(self, obj):
        """把值写成紧凑 JSON（与游戏格式一致，保持字段顺序）。"""
        self.value_bytes = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def parse_dlpn(data):
    """解析 DLPN 二进制容器，返回 (version, entries 列表)。"""
    if not data.startswith(DLPN_MAGIC):
        raise ValueError("不是 DLPN 容器")
    pos = 4
    version, cnt = struct.unpack_from("<II", data, pos)
    pos += 8
    entries = []
    for _ in range(cnt):
        klen = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        key_bytes = data[pos:pos + klen]
        pos += klen
        type_id = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        vlen = struct.unpack_from("<I", data, pos)[0]
        pos += 4
        value_bytes = data[pos:pos + vlen]
        pos += vlen
        entries.append(SaveEntry(key_bytes, type_id, value_bytes))
    return version, entries


def build_dlpn(version, entries):
    """重新打包 DLPN 容器。"""
    out = bytearray(DLPN_MAGIC)
    out += struct.pack("<II", version, len(entries))
    for e in entries:
        out += struct.pack("<I", len(e.key_bytes))
        out += e.key_bytes
        out += struct.pack("<I", e.type_id)
        out += struct.pack("<I", len(e.value_bytes))
        out += e.value_bytes
    return bytes(out)


# ============================== 存档文件 ==============================

def find_saves_dir(explicit=None):
    """定位游戏存档目录 SavesDir。"""
    if explicit:
        d = os.path.join(explicit, "SavesDir") if not explicit.endswith("SavesDir") else explicit
        if os.path.isdir(d):
            return d
        if os.path.isdir(explicit):
            return explicit
        return None
    # 自动搜索：1) 脚本同目录 2) 常见 Steam 路径
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.isdir(os.path.join(here, "SavesDir")):
        return os.path.join(here, "SavesDir")
    for gd in GAME_DIR_CANDIDATES:
        d = os.path.join(gd, "SavesDir")
        if os.path.isdir(d):
            return d
    return None


def list_slots(saves_dir):
    """列出存档槽位（*_One.sav 等主档对应关系由 owner 文件体现）。"""
    owners = sorted(glob.glob(os.path.join(saves_dir, "*_*.owner.sav")))
    slots = []
    for p in owners:
        name = os.path.basename(p)
        # 形如 <steamid>_<Slot>.owner.sav
        slots.append(name)
    return slots


def decrypt_owner_file(path):
    """读取 owner 存档：文件 = XXTEA(明文)，明文 = DLPN 容器。"""
    raw = open(path, "rb").read()
    plain = xxtea_decrypt(raw, SAVE_KEY)
    if not plain.startswith(DLPN_MAGIC):
        raise ValueError("解密结果不是 DLPN 容器，请检查存档路径")
    version, entries = parse_dlpn(plain)
    return version, entries


def encrypt_owner_file(version, entries):
    """DLPN 容器 -> XXTEA -> 密文字节。"""
    plain = build_dlpn(version, entries)
    return xxtea_encrypt(plain, SAVE_KEY)


def backup_file(path):
    """写回前自动备份。"""
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    bak = path + f".bak_{ts}"
    shutil.copy2(path, bak)
    return bak


def write_owner_file(path, version, entries, do_backup=True):
    """写回存档。先备份，再写，再回读校验。"""
    bak = backup_file(path) if do_backup else None
    cipher = encrypt_owner_file(version, entries)
    with open(path, "wb") as f:
        f.write(cipher)
    # 回读校验
    v2, e2 = decrypt_owner_file(path)
    ok = (v2 == version and len(e2) == len(entries))
    if not ok:
        raise RuntimeError("写回后校验失败！已保留备份，请手动恢复：" + bak)
    return bak


# ============================== 修改辅助 ==============================

def get_entry(entries, key):
    for e in entries:
        if e.key == key:
            return e
    return None


def show_summary(entries):
    """打印存档核心数据摘要。"""
    print("\n===== 存档核心数据摘要 =====")
    lines = []
    sd = get_entry(entries, "safe-deposit.resources")
    if sd:
        d = sd.json_value()
        if d:
            lines.append(("保险库存款（基地/死后保留）", f"金币={d.get('coin')}  炸弹={d.get('bomb')}  钥匙={d.get('key')}  蛋={d.get('egg')}"))
    tb = get_entry(entries, "token.balance")
    if tb:
        d = tb.json_value()
        if d:
            toks = d.get("entries", [])
            tstr = ", ".join(f"类型{k['key']}={k['value']}" for k in toks) or "无"
            lines.append(("霓虹币 Token", tstr))
    ev = get_entry(entries, "evolution.progression")
    if ev:
        d = ev.json_value()
        if d:
            lines.append(("进化系统", f"等级={d.get('level')}  经验={d.get('currentExperience')}  可用点数={d.get('cachedAvailablePoints')}"))
    cu = get_entry(entries, "profile.character-unlock")
    if cu:
        d = cu.json_value()
        if d:
            lines.append(("已解锁角色数", str(len(d.get("ids", [])))))
    pu = get_entry(entries, "progression.content-unlock")
    if pu:
        d = pu.json_value()
        if d:
            lines.append(("已解锁内容数", str(len(d.get("ids", [])))))
    ft = get_entry(entries, "profile.game-time")
    if ft:
        d = ft.json_value()
        if d:
            lines.append(("累计游戏时长(秒)", str(round(d.get("seconds", 0)))))
    rc = get_entry(entries, "network.run-count")
    if rc:
        d = rc.json_value()
        if d:
            lines.append(("游玩局数", str(d.get("count"))))
    for k, v in lines:
        print(f"  {k:<20}: {v}")
    print("==============================")


def menu_modify_resource(entries):
    """修改保险库资源：金币/炸弹/钥匙/蛋。"""
    sd = get_entry(entries, "safe-deposit.resources")
    if not sd:
        print("未找到 safe-deposit.resources 条目！")
        return
    d = sd.json_value()
    if not d:
        print("该条目不是 JSON，无法修改。")
        return
    print("\n当前保险库存款：")
    print(f"  金币 coin   = {d.get('coin', 0)}")
    print(f"  炸弹 bomb   = {d.get('bomb', 0)}")
    print(f"  钥匙 key    = {d.get('key', 0)}")
    print(f"  蛋   egg    = {d.get('egg', 0)}")
    print("（提示：这是基地保险柜里的存款，死亡后仍保留。局内捡的会另外算。）")
    fields = {"1": ("coin", "金币"), "2": ("bomb", "炸弹"), "3": ("key", "钥匙"), "4": ("egg", "蛋")}
    while True:
        ch = input("\n要改哪一项？[1金币 2炸弹 3钥匙 4蛋 0返回] ").strip()
        if ch == "0":
            return
        if ch not in fields:
            print("无效选择。")
            continue
        fname, fcn = fields[ch]
        try:
            nv = int(input(f"输入新的{ fcn }数量（建议 0~99999999）：").strip())
        except ValueError:
            print("请输入整数。")
            continue
        if nv < 0:
            nv = 0
        d[fname] = nv
        sd.set_json(d)
        print(f"已修改：{fcn} -> {nv}（保存时生效）")
        return


def menu_modify_token(entries):
    """修改霓虹币 Token 余额。"""
    tb = get_entry(entries, "token.balance")
    if not tb:
        print("未找到 token.balance 条目！")
        return
    d = tb.json_value()
    if not d:
        print("该条目不是 JSON。")
        return
    toks = d.setdefault("entries", [])
    print("\n当前 Token 余额：")
    for t in toks:
        print(f"  key={t.get('key')}  value={t.get('value')}")
    try:
        val = int(input("\n输入新的 Token 数量（key=1 为主货币，建议 0~99999999）：").strip())
    except ValueError:
        print("请输入整数。")
        return
    if val < 0:
        val = 0
    if toks:
        toks[0]["value"] = val
    else:
        toks.append({"key": 1, "value": val})
    tb.set_json(d)
    print(f"已修改 Token -> {val}")


def menu_modify_evolution(entries):
    """修改进化等级/经验。"""
    ev = get_entry(entries, "evolution.progression")
    if not ev:
        print("未找到 evolution.progression 条目！")
        return
    d = ev.json_value()
    if not d:
        print("该条目不是 JSON。")
        return
    print("\n当前进化状态：")
    print(f"  等级 level                = {d.get('level')}")
    print(f"  当前经验 currentExperience = {d.get('currentExperience')}")
    print(f"  可用点数 cachedAvailablePoints = {d.get('cachedAvailablePoints')}")
    try:
        lv = int(input("\n输入新的等级（建议 1~999）：").strip())
        exp = int(input("输入新的当前经验（0 表示满级/无经验）：").strip())
    except ValueError:
        print("请输入整数。")
        return
    d["level"] = max(1, lv)
    d["currentExperience"] = max(0, exp)
    ev.set_json(d)
    print(f"已修改：等级 -> {d['level']}，经验 -> {d['currentExperience']}")


def menu_add_unlock(entries, key, name):
    """向 ids 列表追加解锁 ID。"""
    en = get_entry(entries, key)
    if not en:
        print(f"未找到 {key} 条目！")
        return
    d = en.json_value()
    if not d:
        print("该条目不是 JSON。")
        return
    ids = d.setdefault("ids", [])
    print(f"\n当前{name}数：{len(ids)}，前 20 个：{ids[:20]}")
    try:
        raw = input(f"输入要追加的{name} ID（多个用逗号分隔，输入 0 返回）：").strip()
    except Exception:
        return
    if raw in ("0", ""):
        return
    add = []
    for part in raw.replace("，", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            v = int(part)
        except ValueError:
            print(f"忽略非数字：{part}")
            continue
        if v not in ids:
            add.append(v)
    if add:
        ids.extend(add)
        en.set_json(d)
        print(f"已追加 {len(add)} 个 ID，当前总数 {len(ids)}")
    else:
        print("没有新 ID 可加（可能已存在）。")


def menu_show_statistics(entries):
    """展示统计信息（只读）。"""
    st = get_entry(entries, "statistics.game-all")
    if not st:
        print("未找到统计条目。")
        return
    d = st.json_value()
    if not d:
        print("统计条目不是 JSON，无法展示。")
        return
    names = {n["key"]: n["value"] for n in d.get("names", [])}
    vals = d.get("values", d.get("data", []))
    print("\n===== 全局统计（只读摘要） =====")
    # 尝试匹配 names 与 values
    cnt = 0
    if isinstance(vals, list):
        for item in vals:
            if isinstance(item, dict) and "key" in item:
                k = item["key"]
                nm = names.get(k, str(k))
                print(f"  {nm}: {item.get('value')}")
                cnt += 1
                if cnt >= 40:
                    print("  ...（仅显示前 40 条）")
                    break
    if cnt == 0:
        print("  结构暂不支持展示，但数据完好。")
    print("==============================")


def menu_restore_backup(saves_dir, owner_path):
    """列出备份并恢复。"""
    baks = sorted(glob.glob(owner_path + ".bak_*"))
    if not baks:
        print("没有找到备份文件。")
        return
    print("\n可恢复的备份：")
    for i, b in enumerate(baks, 1):
        print(f"  {i}. {os.path.basename(b)}")
    try:
        ch = int(input("选择要恢复的备份编号（0 取消）：").strip())
    except ValueError:
        return
    if ch == 0 or ch > len(baks):
        return
    src = baks[ch - 1]
    shutil.copy2(src, owner_path)
    print(f"已恢复：{os.path.basename(src)} -> {os.path.basename(owner_path)}")


# ============================== 主流程 ==============================

def main():
    print("=" * 56)
    print("  《霓虹深渊2》存档修改器  v1.0")
    print("  仅修改本地存档，不改内存、不碰游戏文件")
    print("=" * 56)

    import argparse
    ap = argparse.ArgumentParser(description="霓虹深渊2 存档修改器")
    ap.add_argument("--dir", help="游戏存档目录（SavesDir）路径，不填则自动查找")
    ap.add_argument("--slot", default=None, help="存档槽位：One / Two / Three / Network，不填则让用户选择")
    args = ap.parse_args()

    saves_dir = find_saves_dir(args.dir)
    if not saves_dir:
        print("未找到 SavesDir 存档目录。请用参数指定，例如：")
        print("  python neon_save_editor.py --dir \"D:\\Steam\\steamapps\\common\\Neon Abyss 2\"")
        return
    print(f"存档目录：{saves_dir}")

    slots = list_slots(saves_dir)
    if not slots:
        print("没有发现任何 *_*.owner.sav 存档。请确认存档存在（先运行一次游戏）。")
        return
    print("\n发现存档槽位：")
    for i, s in enumerate(slots, 1):
        print(f"  {i}. {s}")

    # 选择槽位
    idx = None
    if args.slot:
        for i, s in enumerate(slots, 1):
            if s.upper().startswith(args.slot.upper()):
                idx = i
                break
        if idx is None:
            print(f"未找到槽位 {args.slot}，自动使用第一个。")
    if idx is None:
        try:
            ch = int(input("\n选择存档槽位编号（1~%d）：" % len(slots)).strip())
            if 1 <= ch <= len(slots):
                idx = ch
        except ValueError:
            pass
    if idx is None:
        idx = 1
    slot_name = slots[idx - 1]
    owner_path = os.path.join(saves_dir, slot_name)
    print(f"已选择：{slot_name}")

    # 读取
    version, entries = decrypt_owner_file(owner_path)
    print(f"存档解密成功：版本 {version}，数据键 {len(entries)} 个")
    show_summary(entries)

    while True:
        print("\n请选择操作：")
        print("  1. 修改保险库资源（金币/炸弹/钥匙/蛋）")
        print("  2. 修改霓虹币 Token 余额")
        print("  3. 修改进化等级 / 经验")
        print("  4. 追加解锁角色 ID")
        print("  5. 追加解锁内容 ID")
        print("  6. 查看统计信息（只读）")
        print("  7. 恢复备份")
        print("  8. 保存并退出（写回存档）")
        print("  0. 不保存直接退出")
        try:
            ch = input(">>> ").strip()
        except (EOFError, KeyboardInterrupt):
            ch = "0"
        if ch == "1":
            menu_modify_resource(entries)
        elif ch == "2":
            menu_modify_token(entries)
        elif ch == "3":
            menu_modify_evolution(entries)
        elif ch == "4":
            menu_add_unlock(entries, "profile.character-unlock", "角色")
        elif ch == "5":
            menu_add_unlock(entries, "progression.content-unlock", "内容")
        elif ch == "6":
            menu_show_statistics(entries)
        elif ch == "7":
            menu_restore_backup(saves_dir, owner_path)
        elif ch == "8":
            try:
                bak = write_owner_file(owner_path, version, entries, do_backup=True)
                print("\n✔ 已保存！")
                print(f"  备份文件：{bak}")
                print(f"  存档文件：{owner_path}")
                print("建议：先备份的副本不要删；如果游戏里没生效，可用选项7恢复。")
            except Exception as e:
                print("\n保存失败：", e)
            return
        elif ch == "0":
            print("未保存任何修改，退出。")
            return
        else:
            print("无效选择，请重新输入。")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
《霓虹深渊2》游戏内修改 —— 内存模块
=====================================
基于 ctypes + ReadProcessMemory / WriteProcessMemory 附加 NeonAbyss2.exe。
定位策略（IL2CPP 符号驱动）：
  1) 解析 GameAssembly.dll PE 导出表，获取 il2cpp_* 运行时 API；
  2) il2cpp_class_from_name 拿到目标类（NEONPlayerState / PlayerActor /
     EggMinionActor / HitPoints）的 Il2CppClass*；
  3) 内存扫描引用该 klass 指针的对象（IL2CPP 对象头 offset 0 = klass 指针），
     定位运行时单例实例；
  4) 字段直写（无敌/重力/连跳/速度/CD/伤害等固定偏移）；
  5) 资源（金钱/炸弹/钥匙/水晶/生命/护盾等）通过 NEONPlayerState.attrs 的
     AttriConfigDict._fixedValueCache 字典（key -> double）读写。

受限项说明见 README：无法可靠定位的（游戏速度 Time.timeScale 为 icall、
进化点数/命运卡片/角色解锁为局外存档数据）如实标注，不做盲写。

作者：Marvis File Agent
"""

import ctypes
import ctypes.wintypes as wt
import os
import struct
import threading
import time

try:
    import pymem  # 可选：安装了 pymem 则优先走它的 Pymem 封装，否则纯 ctypes
    HAS_PYMEM = True
except Exception:
    HAS_PYMEM = False

PROCESS_NAME = "NeonAbyss2.exe"


def _resolve_game_dir():
    """从 Steam 注册表动态获取游戏目录，避免硬编码本地绝对路径。

    读取 HKEY_CURRENT_USER\\Software\\Valve\\Steam 的 SteamPath，
    拼接 \\steamapps\\common\\Neon Abyss 2；注册表读取失败或目录不存在时回退空串。
    """
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam") as key:
            steam_path = winreg.QueryValueEx(key, "SteamPath")[0]
        game_dir = os.path.join(steam_path, "steamapps", "common", "Neon Abyss 2")
        if os.path.isdir(game_dir):
            return game_dir
    except Exception:
        pass
    return ""


GAME_DIR = _resolve_game_dir()
GAME_ASSEMBLY = GAME_DIR + r"\GameAssembly.dll" if GAME_DIR else ""

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
u32 = ctypes.WinDLL("user32", use_last_error=True)

PROCESS_ALL_ACCESS = 0x1F0FFF
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000
PAGE_READWRITE = 0x04
PAGE_EXECUTE_READWRITE = 0x40
PAGE_WRITECOPY = 0x08


class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wt.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wt.DWORD),
        ("Protect", wt.DWORD),
        ("Type", wt.DWORD),
    ]


def _read_bytes(h, addr, size):
    buf = ctypes.create_string_buffer(size)
    n = ctypes.c_size_t(0)
    if not k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(n)):
        return None
    return buf.raw[:n.value]


def _write_bytes(h, addr, data):
    n = ctypes.c_size_t(0)
    return bool(k32.WriteProcessMemory(h, ctypes.c_void_p(addr), data, len(data), ctypes.byref(n)))


class NeonProcess:
    """封装附加进程 + IL2CPP 符号 + 内存读写。"""

    def __init__(self):
        self.h = None
        self.pid = None
        self.module_base = None
        self.module_size = 0
        self.api = {}
        self._class_cache = {}

    # ---------------- 附加 ----------------
    def attach(self, pid=None):
        if pid:
            self.pid = pid
        else:
            self.pid = self.find_pid()
        if not self.pid:
            raise RuntimeError("未找到游戏进程 NeonAbyss2.exe，请先启动游戏")
        self.h = k32.OpenProcess(PROCESS_ALL_ACCESS, False, self.pid)
        if not self.h:
            raise RuntimeError(f"OpenProcess 失败（错误码 {ctypes.get_last_error()}），请以管理员身份运行")
        self.module_base = self._find_module()
        if not self.module_base:
            raise RuntimeError("未找到 GameAssembly.dll 模块")
        self._load_il2cpp_api()
        return True

    def find_pid(self):
        # 通过进程快照查找
        import ctypes.wintypes as wt
        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                        ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD),
                        ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
                        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                        ("szExeFile", ctypes.c_wchar * 260)]
        TH32CS_SNAPPROCESS = 0x2
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snap == -1:
            return None
        pe = PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        if k32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                if pe.szExeFile.lower() == PROCESS_NAME.lower():
                    k32.CloseHandle(snap)
                    return pe.th32ProcessID
                if not k32.Process32NextW(snap, ctypes.byref(pe)):
                    break
        k32.CloseHandle(snap)
        return None

    def _find_module(self):
        try:
            if HAS_PYMEM:
                pm = pymem.Pymem(self.pid)
                return pm.process_base
        except Exception:
            pass
        return self._module_from_snapshot()

    def _module_from_snapshot(self):
        import ctypes.wintypes as wt
        class MODULEENTRY32W(ctypes.Structure):
            _fields_ = [("dwSize", wt.DWORD), ("th32ModuleID", wt.DWORD),
                        ("th32ProcessID", wt.DWORD), ("GlblcntUsage", wt.DWORD),
                        ("ProccntUsage", wt.DWORD), ("modBaseAddr", ctypes.c_void_p),
                        ("modBaseSize", wt.DWORD), ("hModule", ctypes.c_void_p),
                        ("szModule", ctypes.c_wchar * 256), ("szExePath", ctypes.c_wchar * 260)]
        TH32CS_SNAPMODULE = 0x8
        snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | 0x10, self.pid)
        if snap == -1:
            return None
        me = MODULEENTRY32W()
        me.dwSize = ctypes.sizeof(MODULEENTRY32W)
        base = None
        if k32.Module32FirstW(snap, ctypes.byref(me)):
            while True:
                if me.szModule.lower() == "gameassembly.dll":
                    base = me.modBaseAddr
                    self.module_size = me.modBaseSize
                    break
                if not k32.Module32NextW(snap, ctypes.byref(me)):
                    break
        k32.CloseHandle(snap)
        return base

    # ---------------- IL2CPP API 解析（PE 导出表） ----------------
    def _load_il2cpp_api(self):
        dos = _read_bytes(self.h, self.module_base, 0x40)
        if not dos or dos[:2] != b"MZ":
            raise RuntimeError("GameAssembly.dll 模块头无效")
        e_lfanew = struct.unpack_from("<I", dos, 0x3C)[0]
        nt = _read_bytes(self.h, self.module_base + e_lfanew, 0x100)
        if not nt or nt[:4] != b"PE\x00\x00":
            raise RuntimeError("PE 头无效")
        opt_magic = struct.unpack_from("<H", nt, 24)[0]
        opt_size = struct.unpack_from("<H", nt, 20)[0]
        num_sections = struct.unpack_from("<H", nt, 6)[0]
        exp_rva_off = 112 if opt_magic == 0x20B else 96  # x64 可选头中导出表 RVA 偏移
        dd_rva, dd_size = struct.unpack_from("<II", nt, exp_rva_off)
        # 加载节表做 RVA->VA 映射
        sec_off = e_lfanew + 24 + opt_size
        sec_data = _read_bytes(self.h, self.module_base + sec_off, num_sections * 40)
        if not sec_data:
            raise RuntimeError("节表读取失败")
        sections = []
        for i in range(num_sections):
            sec = sec_data[i * 40:(i + 1) * 40]
            vsize, va, rsize, raw = struct.unpack_from("<IIII", sec, 8)
            sections.append((va, vsize, raw))

        def rva_to_addr(rva):
            for va, vsize, raw in sections:
                if va <= rva < va + vsize:
                    return self.module_base + (rva - va)
            return None

        def parse_export(exp_rva):
            """解析导出表并填充 self.api；无效返回 False。"""
            exp_addr = rva_to_addr(exp_rva)
            exp = _read_bytes(self.h, exp_addr, 0x1000) if exp_addr else None
            if not exp or len(exp) < 40:
                return False
            num_names = struct.unpack_from("<I", exp, 24)[0]
            if not (0 < num_names <= 200000):
                return False
            addr_of_names = struct.unpack_from("<I", exp, 32)[0]
            addr_of_functions = struct.unpack_from("<I", exp, 36)[0]
            addr_of_name_ordinals = struct.unpack_from("<I", exp, 40)[0]
            names_base = rva_to_addr(addr_of_names)
            funcs_base = rva_to_addr(addr_of_functions)
            ords_base = rva_to_addr(addr_of_name_ordinals)
            if not (names_base and funcs_base and ords_base):
                return False
            names_data = _read_bytes(self.h, names_base, num_names * 4)
            ords_data = _read_bytes(self.h, ords_base, num_names * 2)
            if not names_data or not ords_data:
                return False
            api = {}
            for i in range(num_names):
                name_rva = struct.unpack_from("<I", names_data, i * 4)[0]
                ordinal = struct.unpack_from("<H", ords_data, i * 2)[0]
                name = self._read_cstr(rva_to_addr(name_rva))
                if not name:
                    continue
                func_bytes = _read_bytes(self.h, funcs_base + ordinal * 4, 4)
                if not func_bytes:
                    continue
                func_rva = struct.unpack_from("<I", func_bytes, 0)[0]
                addr = rva_to_addr(func_rva)
                if addr:
                    api[name] = addr
            self.api = api
            return True

        # 1) 优先 DataDirectory[0]
        ok = parse_export(dd_rva) if dd_rva else False
        # 2) fallback：本游戏已验证的导出表 RVA（DataDirectory[0] 被 IL2CPP 混淆为假值）
        if not ok:
            ok = parse_export(0x50A7A00)
        if not ok:
            raise RuntimeError("导出表解析失败（DataDirectory 与 fallback 均无效）")
        for need in ("il2cpp_domain_get_assemblies", "il2cpp_assembly_get_image",
                     "il2cpp_class_from_name", "il2cpp_class_get_fields",
                     "il2cpp_field_get_offset", "il2cpp_field_static_get_value",
                     "il2cpp_field_static_set_value", "il2cpp_class_get_methods",
                     "il2cpp_method_get_name", "il2cpp_method_get_param_count",
                     "il2cpp_runtime_invoke", "il2cpp_string_new",
                     "il2cpp_class_from_type", "il2cpp_domain_get", "il2cpp_value_box"):
            if need not in self.api:
                raise RuntimeError(f"GameAssembly.dll 缺少导出 API: {need}")

    def _read_cstr(self, addr, max_len=256):
        if not addr:
            return None
        buf = _read_bytes(self.h, addr, max_len)
        if not buf:
            return None
        return buf.split(b"\x00")[0].decode("utf-8", "replace")

    def _read_il2cpp_string(self, addr):
        """读 Il2CppString：offset 0x10 是 length(int32)，0x18 起 UTF-16 字符（x64）。"""
        if not addr:
            return None
        head = _read_bytes(self.h, addr, 0x18)
        if not head:
            return None
        length = struct.unpack_from("<i", head, 0x10)[0]
        if length < 0 or length > 4096:
            return None
        data = _read_bytes(self.h, addr + 0x18, length * 2)
        if data is None:
            return None
        return data.decode("utf-16-le", "replace")

    # ---------------- IL2CPP 高层封装 ----------------
    def _call(self, name, restype, argtypes, *args):
        fn = ctypes.CFUNCTYPE(restype, *argtypes)(self.api[name])
        return fn(*args)

    def domain_get_assemblies(self):
        dom = self._call("il2cpp_domain_get", ctypes.c_void_p)()
        n = ctypes.c_size_t(0)
        f = self._call("il2cpp_domain_get_assemblies", ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t))
        return f(dom, ctypes.byref(n)), n

    def assembly_get_image(self, assembly_ptr):
        return self._call("il2cpp_assembly_get_image", ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(assembly_ptr)

    def class_from_name(self, namespaze, name):
        key = (namespaze, name)
        if key in self._class_cache:
            return self._class_cache[key]
        assemblies_ptr, n = self.domain_get_assemblies()
        if not assemblies_ptr:
            return None
        fn = self._call("il2cpp_class_from_name", ctypes.c_void_p,
                        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p)
        ns = namespaze.encode("utf-8") if namespaze else b""
        nb = name.encode("utf-8")
        cls = None
        cnt = n.value if n.value < 4096 else 4096
        arr = (ctypes.c_void_p * cnt).from_address(assemblies_ptr)
        for i in range(cnt):
            img = self.assembly_get_image(arr[i])
            if not img:
                continue
            cls = fn(img, ns, nb)
            if cls:
                break
        self._class_cache[key] = cls
        return cls

    def class_get_fields(self, klass):
        """返回 [(name, offset)] 字段列表（含静态标记）。"""
        fn = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
            self.api["il2cpp_class_get_fields"])
        get_name = ctypes.CFUNCTYPE(ctypes.c_char_p, ctypes.c_void_p)(
            self.api.get("il2cpp_field_get_name", 0) or 0)
        # 若没有 il2cpp_field_get_name，则不返回名字（运行时仅需偏移）
        out = []
        if not get_name:
            return out
        get_offset = ctypes.CFUNCTYPE(ctypes.c_size_t, ctypes.c_void_p)(
            self.api["il2cpp_field_get_offset"])
        iter_ = ctypes.c_void_p(0)
        while True:
            fld = fn(klass, ctypes.byref(iter_))
            if not fld:
                break
            nm = get_name(fld)
            name = nm.decode("utf-8", "replace") if nm else "?"
            off = get_offset(fld)
            out.append((name, off))
        return out

    def class_get_methods(self, klass):
        """返回 [(name, param_count, MethodInfo*)]。"""
        fn = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
            self.api["il2cpp_class_get_methods"])
        get_name = ctypes.CFUNCTYPE(ctypes.c_char_p, ctypes.c_void_p)(self.api["il2cpp_method_get_name"])
        get_pc = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p)(self.api["il2cpp_method_get_param_count"])
        out = []
        iter_ = ctypes.c_void_p(0)
        while True:
            m = fn(klass, ctypes.byref(iter_))
            if not m:
                break
            nm = get_name(m)
            name = nm.decode("utf-8", "replace") if nm else "?"
            out.append((name, get_pc(m), m))
        return out

    def find_method(self, klass, want_name, want_pc=None):
        for name, pc, m in self.class_get_methods(klass):
            if name == want_name and (want_pc is None or pc == want_pc):
                return m
        return None

    def runtime_invoke(self, method, obj, params=None):
        """调用 IL2CPP 方法。params 为 c_void_p 参数对象数组（已装箱对象）。"""
        exc = ctypes.c_void_p(0)
        arr = None
        if params:
            arr = (ctypes.c_void_p * len(params))(*params)
            params_ptr = ctypes.cast(arr, ctypes.POINTER(ctypes.c_void_p))
        else:
            params_ptr = None
        fn = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
                              ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p))(
            self.api["il2cpp_runtime_invoke"])
        return fn(method, obj, params_ptr, ctypes.byref(exc))

    # ---------------- 内存读写 ----------------
    def read_ptr(self, addr):
        d = _read_bytes(self.h, addr, 8)
        return struct.unpack("<Q", d)[0] if d and len(d) == 8 else 0

    def read_u64(self, addr):
        return self.read_ptr(addr)

    def read_i32(self, addr):
        d = _read_bytes(self.h, addr, 4)
        return struct.unpack("<i", d)[0] if d and len(d) == 4 else 0

    def read_u32(self, addr):
        d = _read_bytes(self.h, addr, 4)
        return struct.unpack("<I", d)[0] if d and len(d) == 4 else 0

    def read_f32(self, addr):
        d = _read_bytes(self.h, addr, 4)
        return struct.unpack("<f", d)[0] if d and len(d) == 4 else 0.0

    def read_bool(self, addr):
        d = _read_bytes(self.h, addr, 1)
        return bool(d and d[0])

    def write_i32(self, addr, val):
        return _write_bytes(self.h, addr, struct.pack("<i", int(val)))

    def write_f32(self, addr, val):
        return _write_bytes(self.h, addr, struct.pack("<f", float(val)))

    def write_bool(self, addr, val):
        return _write_bytes(self.h, addr, bytes([1 if val else 0]))

    def write_double(self, addr, val):
        return _write_bytes(self.h, addr, struct.pack("<d", float(val)))

    # ---------------- 实例定位（klass 指针扫描） ----------------
    def scan_instances(self, klass_ptr, limit=200):
        """扫描进程可读私有内存区，查找对象头 klass 指针等于 klass_ptr 的实例地址。"""
        found = []
        addr = 0
        mbi = MEMORY_BASIC_INFORMATION()
        while k32.VirtualQueryEx(self.h, ctypes.c_void_p(addr), ctypes.byref(mbi),
                                 ctypes.sizeof(mbi)):
            if mbi.State == MEM_COMMIT and mbi.Type == MEM_PRIVATE and \
               (mbi.Protect in (PAGE_READWRITE, PAGE_EXECUTE_READWRITE, PAGE_WRITECOPY)):
                region = _read_bytes(self.h, mbi.BaseAddress, min(mbi.RegionSize, 0x20000000))
                if region and len(region) >= 8:
                    # 8 字节对齐扫描
                    start = (int(mbi.BaseAddress) + 7) & ~7
                    off = start - int(mbi.BaseAddress)
                    pat = struct.pack("<Q", klass_ptr)
                    base = pat[0]
                    i = off
                    data = region
                    while i <= len(data) - 8:
                        if data[i] == base and data[i:i + 8] == pat:
                            found.append(int(mbi.BaseAddress) + i)
                            if len(found) >= limit:
                                break
                        i += 8
            addr = int(mbi.BaseAddress) + mbi.RegionSize
            if addr <= 0:
                break
            if addr > 0x7FFFFFFFFFFFFFFF:
                break
        return found

    # ---------------- Attri 字典（资源权威存储） ----------------
    def read_attr_dict(self, attrs_ptr):
        """读 Attri 的 _fixedValueCache（Dictionary<string,double>），返回 {key: value}。"""
        # Attri 布局：_fixedValueCache @0x68
        if not attrs_ptr:
            return {}
        d = self.read_ptr(attrs_ptr + 0x68)
        return self.read_string_double_dict(d)

    def read_string_double_dict(self, dict_ptr):
        """Unity Dictionary<string, double>：buckets @0x10, entries @0x18, count @0x20。"""
        if not dict_ptr:
            return {}
        entries_ptr = self.read_ptr(dict_ptr + 0x18)
        count = self.read_i32(dict_ptr + 0x20)
        if not entries_ptr or count <= 0 or count > 4096:
            return {}
        out = {}
        for i in range(count):
            base = entries_ptr + i * 32
            key_ptr = self.read_ptr(base + 0x8)
            if not key_ptr:
                continue
            key = self._read_il2cpp_string(key_ptr)
            val = struct.unpack("<d", _read_bytes(self.h, base + 0x10, 8) or b"\x00" * 8)[0]
            if key:
                out[key] = val
        return out

    def write_attr(self, attrs_ptr, key, value):
        """在 attrs 的 FixedValueDict 中修改指定 key 的值；key 不存在返回 False。"""
        if not attrs_ptr:
            return False
        d = self.read_ptr(attrs_ptr + 0x68)
        if not d:
            return False
        entries_ptr = self.read_ptr(d + 0x18)
        count = self.read_i32(d + 0x20)
        if not entries_ptr or count <= 0 or count > 4096:
            return False
        target = key.encode("utf-16-le")
        for i in range(count):
            base = entries_ptr + i * 32
            key_ptr = self.read_ptr(base + 0x8)
            if not key_ptr:
                continue
            length = self.read_i32(key_ptr + 0x10)
            if length != len(key):
                continue
            data = _read_bytes(self.h, key_ptr + 0x18, length * 2)
            if data == target:
                return self.write_double(base + 0x10, float(value))
        return False

    # ---------------- 进程级解析：常用实例缓存 ----------------
    def locate_instances(self):
        """定位全部关键实例，返回 dict（失败项为 None）。"""
        res = {}
        try:
            ps = self.class_from_name("NEON.Game.Managers", "NEONPlayerState")
            res["player_state_class"] = ps
            res["player_state"] = self.scan_instances(ps)[0] if ps else None
        except Exception as e:
            res["player_state"] = None
        try:
            pa = self.class_from_name("NEON.Game.Actors", "PlayerActor")
            res["player_actor_class"] = pa
            res["player_actor"] = self.scan_instances(pa)[0] if pa else None
        except Exception:
            res["player_actor"] = None
        try:
            eg = self.class_from_name("NEON.Game", "EggMinionActor")
            res["egg_class"] = eg
            res["eggs"] = self.scan_instances(eg) if eg else []
        except Exception:
            res["eggs"] = []
        return res


class CheatSpec:
    """一项游戏内修改的定义。"""

    def __init__(self, fid, label, kind, desc, target="player_actor", offset=0,
                 default=0.0, attr_key=None, scale=1.0, group="常规"):
        self.fid = fid          # 唯一 ID
        self.label = label      # 界面显示名
        self.kind = kind        # bool / float / int / attr
        self.desc = desc        # 实现说明
        self.target = target    # player_actor / player_state / egg
        self.offset = offset    # 实例内偏移
        self.default = default  # 默认锁定值
        self.attr_key = attr_key
        self.scale = scale
        self.group = group
        self.locked = False     # 当前是否启用锁定
        self.custom = None      # 用户自定义值


# ============================== 功能规格清单 ==============================
# 说明：偏移来自 Cpp2IL 还原符号（D:\Tools\Cpp2IL\output\DiffableCs\Assembly-CSharp）
# PlayerActor / NEONPlayerState / EggMinionActor 的 Field offset。

CHEATS = [
    # ---- 生存 ----
    CheatSpec("god", "无敌模式", "bool",
              "PlayerActor.normalGodLike@0x310 + continuousGodLike@0x311 锁 True（免疫伤害判定）",
              "player_actor", 0x310, 1.0),
    CheatSpec("hp", "无限生命", "attr",
              "attrs['Heart'] 锁定为 MaxHearts（当前心数恒满）",
              "player_state", 0, attr_key="Heart", group="生存"),
    CheatSpec("shield", "无限护盾", "attr",
              "attrs['Shield'] 锁定为 MaxShields（护盾恒满）",
              "player_state", 0, attr_key="Shield", group="生存"),
    CheatSpec("mana", "无限水晶(法力)", "attr",
              "attrs['Mana'] 锁定为 MaxManas（法力恒满）",
              "player_state", 0, attr_key="Mana", group="生存"),
    # ---- 战斗 ----
    CheatSpec("firespeed", "快速射击", "float",
              "PlayerActor.cd@0x20 锁 0.01 + NEONPlayerState.ContinueShootCD@0x608 锁 0.05",
              "player_actor", 0x20, 0.01, group="战斗"),
    CheatSpec("bomb", "无限炸弹", "attr",
              "attrs['Bomb'] 锁定为 99",
              "player_state", 0, attr_key="Bomb", default=99.0, group="战斗"),
    CheatSpec("nocooldown", "无视技能冷却", "bool",
              "PlayerActor.SecSkillTimer@0x48C / PassiveSkillTimer@0x490 锁 0（技能立即可用）",
              "player_actor", 0x48C, 0.0, group="战斗"),
    CheatSpec("hatch", "孵蛋必定成功", "bool",
              "EggMinionActor.hatchingSuccessRate@0x2BC 锁 1.0 + canHatchOut@0x298 锁 True（扫描全部蛋实例）",
              "egg", 0x2BC, 1.0, group="战斗"),
    CheatSpec("crit", "100%暴击率", "bool",
              "NEONPlayerState._forceCriticalStrike@0x114 锁 True（强制暴击）",
              "player_state", 0x114, 1.0, group="战斗"),
    CheatSpec("onedmg", "超级伤害(一击必杀)", "bool",
              "PlayerActor.baseDamage@0x28 锁 99999 + maxAdditionDamageMult@0x2B8 锁 99",
              "player_actor", 0x28, 99999.0, group="战斗"),
    CheatSpec("dmgmult", "伤害倍率", "float",
              "PlayerActor.maxAdditionDamageMult@0x2B8 直接写倍率（默认 10 倍）",
              "player_actor", 0x2B8, 10.0, group="战斗"),
    # ---- 资源 ----
    CheatSpec("coin", "编辑金钱", "attr",
              "attrs['Coin'] 直接写指定值",
              "player_state", 0, attr_key="Coin", default=99999.0, group="资源"),
    CheatSpec("coingain", "金钱倍率", "bool",
              "NEONPlayerState.CoinExtraGain@0x318 锁 True + CoinExtraGainLucky@0x31C 锁 5（掉落额外金币）",
              "player_state", 0x318, 1.0, group="资源"),
    CheatSpec("key", "编辑钥匙", "attr",
              "attrs['Key'] 直接写指定值",
              "player_state", 0, attr_key="Key", default=99.0, group="资源"),
    CheatSpec("evolve", "编辑进化点数", "attr",
              "受限：进化点由局外 SaveData.evolution 计算，局内无 attrs 键；请用存档页修改",
              "player_state", 0, attr_key="", group="资源"),
    CheatSpec("hajibao", "开盲盒不消耗闪兔币", "attr",
              "探测 attrs['Hajibao'] 并锁定 99（键名缺失时提示受限）",
              "player_state", 0, attr_key="Hajibao", default=99.0, group="资源"),
    # ---- 命运/解锁 ----
    CheatSpec("fatepoint", "无限命运点数", "attr",
              "受限：命运点为局外 FatePointData 存档数据，局内无 attrs 键；请用存档页/CE 修改",
              "player_state", 0, attr_key="", group="命运"),
    CheatSpec("fatecard", "解锁全部命运卡片", "bool",
              "受限：fatePerkData 存于 SaveData@0x140（局外数据），运行期不随 NEONPlayerState 保存；无法可靠写入",
              "player_state", 0, group="命运"),
    CheatSpec("char", "解锁全部角色", "bool",
              "受限：profile.character-unlock 为存档键；请用存档页「角色解锁」追加",
              "player_state", 0, group="命运"),
    # ---- 移动/全局 ----
    CheatSpec("gamespeed", "设置游戏速度", "float",
              "受限：Time.timeScale 为引擎 icall（无托管静态字段可写），需 CE 定位全局变量后手动锁定",
              "player_state", 0, 1.0, group="移动"),
    CheatSpec("movespeed", "设置移动速度", "float",
              "PlayerActor.Movement@0x378 + airSpeed@0xB8 写速度倍率",
              "player_actor", 0x378, 2.0, group="移动"),
    CheatSpec("jump", "无限连跳", "bool",
              "PlayerActor.HasMultibleJump@0x4A4 锁 True + MultipleJumpCD@0x4A0 锁 0 + canJump@0x18 锁 True",
              "player_actor", 0x4A4, 1.0, group="移动"),
    CheatSpec("gravity", "设置重力", "float",
              "PlayerActor.OriGravityScale@0x284 写重力倍率（1=正常，0.3=低重力）",
              "player_actor", 0x284, 0.5, group="移动"),
]


class TrainerEngine:
    """锁定循环引擎：后台线程按 tick 持续写入已启用项。"""

    def __init__(self, proc: NeonProcess):
        self.proc = proc
        self._stop = threading.Event()
        self._thread = None
        self.instances = None
        self.attrs = None
        self.status = "未附加"

    def locate(self):
        self.instances = self.proc.locate_instances()
        if not self.instances.get("player_state"):
            raise RuntimeError("未定位到 NEONPlayerState 实例（请确保已进入对局/场景）")
        ps = self.instances["player_state"]
        attrs_ptr = self.proc.read_ptr(ps + 0x20)  # PlayerState.attrs @0x20
        self.attrs = attrs_ptr
        return True

    def attr_values(self):
        return self.proc.read_attr_dict(self.attrs)

    def apply_once(self, spec: CheatSpec, custom=None):
        """单次应用一项修改（立即写一次）。"""
        proc = self.proc
        if not self.instances:
            self.locate()
        val = custom if custom is not None else spec.default
        # ---- 生命/护盾/法力（专用：Heart 锁 MaxHearts，不是写固定默认值） ----
        if spec.fid in ("hp", "shield", "mana"):
            if not self.attrs:
                return False, "NEONPlayerState.attrs 未定位"
            d = proc.read_attr_dict(self.attrs)
            if spec.fid == "hp":
                m = d.get("MaxHearts", d.get("MaxHeart", 6))
                k, v = "Heart", m
            elif spec.fid == "shield":
                m = d.get("MaxShields", 3)
                k, v = "Shield", m
            else:
                m = d.get("MaxManas", 100)
                k, v = "Mana", m
            ok = proc.write_attr(self.attrs, k, v)
            return ok, f"attrs['{k}'] -> {v}"
        # ---- 通用 attr 资源 ----
        if spec.kind == "attr":
            key = spec.attr_key
            if not key:
                return False, "受限：无可靠内存目标"
            if not self.attrs:
                return False, "NEONPlayerState.attrs 未定位"
            ok = proc.write_attr(self.attrs, key, val)
            return ok, f"attrs['{key}'] = {val}" if ok else f"attrs 中无 {key} 键（键名可能不同，需 CE 核对）"
        if spec.fid == "god":
            pa = self.instances.get("player_actor")
            if not pa:
                return False, "未定位 PlayerActor"
            ok1 = proc.write_bool(pa + 0x310, True)
            ok2 = proc.write_bool(pa + 0x311, True)
            return (ok1 and ok2), "normalGodLike+continuousGodLike = True"
        if spec.fid == "firespeed":
            pa = self.instances.get("player_actor")
            ps = self.instances.get("player_state")
            if not pa or not ps:
                return False, "未定位玩家实例"
            proc.write_f32(pa + 0x20, 0.01)
            proc.write_f32(ps + 0x608, 0.05)
            return True, "cd=0.01, ContinueShootCD=0.05"
        if spec.fid == "nocooldown":
            pa = self.instances.get("player_actor")
            if not pa:
                return False, "未定位 PlayerActor"
            proc.write_f32(pa + 0x48C, 0.0)
            proc.write_f32(pa + 0x490, 0.0)
            return True, "SecSkillTimer/PassiveSkillTimer = 0"
        if spec.fid == "hatch":
            eggs = self.instances.get("eggs") or []
            if not eggs:
                return False, "场景中暂无蛋实例（孵蛋功能仅蛋在场时生效）"
            n = 0
            for e in eggs:
                if proc.write_f32(e + 0x2BC, 1.0):
                    n += 1
                proc.write_bool(e + 0x298, True)
            return n > 0, f"已锁定 {n} 个蛋实例 hatchingSuccessRate=1.0"
        if spec.fid == "crit":
            ps = self.instances.get("player_state")
            if not ps:
                return False, "未定位 NEONPlayerState"
            proc.write_bool(ps + 0x114, True)
            return True, "_forceCriticalStrike = True"
        if spec.fid == "onedmg":
            pa = self.instances.get("player_actor")
            if not pa:
                return False, "未定位 PlayerActor"
            proc.write_f32(pa + 0x28, 99999.0)
            proc.write_f32(pa + 0x2B8, 99.0)
            return True, "baseDamage=99999, maxAdditionDamageMult=99"
        if spec.fid == "dmgmult":
            pa = self.instances.get("player_actor")
            if not pa:
                return False, "未定位 PlayerActor"
            proc.write_f32(pa + 0x2B8, float(val))
            return True, f"maxAdditionDamageMult = {val}"
        if spec.fid == "coingain":
            ps = self.instances.get("player_state")
            if not ps:
                return False, "未定位 NEONPlayerState"
            proc.write_bool(ps + 0x318, True)
            lucky = float(val) if custom is not None else 5.0
            proc.write_f32(ps + 0x31C, lucky)
            return True, f"CoinExtraGain=True, Lucky={lucky}"
        if spec.fid == "movespeed":
            pa = self.instances.get("player_actor")
            if not pa:
                return False, "未定位 PlayerActor"
            proc.write_f32(pa + 0x378, float(val))
            proc.write_f32(pa + 0xB8, float(val))
            return True, f"Movement/airSpeed = {val}"
        if spec.fid == "jump":
            pa = self.instances.get("player_actor")
            if not pa:
                return False, "未定位 PlayerActor"
            proc.write_bool(pa + 0x4A4, True)
            proc.write_f32(pa + 0x4A0, 0.0)
            proc.write_bool(pa + 0x18, True)
            return True, "HasMultibleJump=True, MultipleJumpCD=0, canJump=True"
        if spec.fid == "gravity":
            pa = self.instances.get("player_actor")
            if not pa:
                return False, "未定位 PlayerActor"
            proc.write_f32(pa + 0x284, float(val))
            return True, f"OriGravityScale = {val}"
        # 受限项
        return False, "受限：无可靠内存目标（详见 README）"

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self):
        try:
            self.locate()
        except Exception as e:
            self.status = f"定位失败：{e}"
            return
        self.status = "锁定中"
        while not self._stop.is_set():
            try:
                for spec in CHEATS:
                    if spec.locked:
                        self.apply_once(spec, spec.custom)
            except Exception:
                pass
            self._stop.wait(0.2)

    def set_locked(self, spec, locked, custom=None):
        spec.locked = locked
        spec.custom = custom
        if locked and not (self._thread and self._thread.is_alive()):
            self.start()


def main():
    """命令行自检：附加进程 -> 定位实例 -> 输出 attrs 键。"""
    print("=== 霓虹深渊2 内存模块自检 ===")
    proc = NeonProcess()
    try:
        proc.attach()
    except Exception as e:
        print(f"[FAIL] {e}")
        return 1
    print(f"[OK] 已附加进程 PID={proc.pid}, GameAssembly.dll base=0x{proc.module_base:X}")
    print(f"[OK] il2cpp API 导出数: {len(proc.api)}")
    eng = TrainerEngine(proc)
    try:
        eng.locate()
        inst = eng.instances
        print(f"[OK] NEONPlayerState 实例: {[hex(x) for x in inst['player_state']] if isinstance(inst.get('player_state'), list) else hex(inst['player_state'] or 0)}")
        print(f"[OK] PlayerActor 实例: {hex(inst.get('player_actor') or 0)}")
        print(f"[OK] EggMinionActor 实例数: {len(inst.get('eggs') or [])}")
        vals = eng.attr_values()
        print(f"[OK] attrs 键数: {len(vals)}")
        interesting = [k for k in ("Coin", "Bomb", "Key", "Mana", "Heart", "MaxHearts", "Shield", "MaxShields", "Hajibao", "Crystal", "Egg")]
        for k in interesting:
            if k in vals:
                print(f"    attrs['{k}'] = {vals[k]}")
    except Exception as e:
        print(f"[WARN] 实例定位失败（可能未进入对局）：{e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

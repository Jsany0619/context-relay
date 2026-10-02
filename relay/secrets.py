"""Windows current-user DPAPI and narrowly scoped private file ACLs.

This protects selected secrets at rest, not a whole task database or against the
same logged-in account, an administrator, malware in this process, or memory dumps.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
from ctypes import wintypes
from functools import lru_cache
import hashlib
import hmac
import os
from pathlib import Path
import shutil
import stat
import uuid


class SecretError(ValueError):
    pass


_MAGIC = b"CRDPAPI\x01"
_LIMIT = 8 * 1024 * 1024


class _Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p), ("inherit", wintypes.BOOL)]


class _AclSize(ctypes.Structure):
    _fields_ = [("count", wintypes.DWORD), ("used", wintypes.DWORD), ("free", wintypes.DWORD)]


@lru_cache(maxsize=1)
def _windows():
    if os.name != "nt":
        raise SecretError("敏感密钥保护需要 Windows 当前用户 DPAPI，不提供明文回退。")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    # Explicit pointer-sized signatures are essential on 64-bit Windows.
    kernel.GetCurrentProcess.argtypes, kernel.GetCurrentProcess.restype = [], wintypes.HANDLE
    kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [wintypes.HANDLE], wintypes.BOOL
    kernel.LocalFree.argtypes, kernel.LocalFree.restype = [ctypes.c_void_p], ctypes.c_void_p
    kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_SecurityAttributes)]
    kernel.CreateDirectoryW.restype = wintypes.BOOL
    advapi.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi.OpenProcessToken.restype = wintypes.BOOL
    advapi.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetTokenInformation.restype = wintypes.BOOL
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    advapi.ConvertSidToStringSidW.restype = wintypes.BOOL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    advapi.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    advapi.SetFileSecurityW.restype = wintypes.BOOL
    advapi.GetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    advapi.GetFileSecurityW.restype = wintypes.BOOL
    advapi.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
    advapi.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi.GetSecurityDescriptorDacl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.BOOL), ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.BOOL)]
    advapi.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int]
    advapi.GetAclInformation.restype = wintypes.BOOL
    advapi.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi.GetAce.restype = wintypes.BOOL
    crypt.CryptProtectData.argtypes = [ctypes.POINTER(_Blob), wintypes.LPCWSTR, ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    crypt.CryptProtectData.restype = wintypes.BOOL
    crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(_Blob)]
    crypt.CryptUnprotectData.restype = wintypes.BOOL
    return kernel, advapi, crypt


def _check(result, message):
    if not result:
        raise SecretError(f"{message}（Windows 错误 {ctypes.get_last_error()}）。")


def _sid_text(sid, kernel, advapi):
    output = ctypes.c_void_p()
    _check(advapi.ConvertSidToStringSidW(sid, ctypes.byref(output)), "无法读取当前用户权限身份")
    try:
        return ctypes.wstring_at(output)
    finally:
        kernel.LocalFree(output)


def _user_sid(kernel, advapi):
    token = wintypes.HANDLE()
    _check(advapi.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)), "无法读取当前用户令牌")
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not size.value:
            raise SecretError("无法确定当前用户令牌大小。")
        buffer = ctypes.create_string_buffer(size.value)
        _check(advapi.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)), "无法读取当前用户令牌")
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        return _sid_text(sid, kernel, advapi)
    finally:
        kernel.CloseHandle(token)


def _local_path(path):
    if os.name != "nt":
        raise SecretError("此密钥存储需要 Windows 当前用户 DPAPI。")
    path = Path(os.path.abspath(path))
    if str(path).startswith("\\\\"):
        raise SecretError("密钥及私有状态必须使用本地目录，不能使用网络共享。")
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise SecretError("敏感路径含符号链接或重解析点，拒绝跟随。")
    return path


@contextmanager
def _descriptor(directory):
    kernel, advapi, _ = _windows()
    inherit = "OICI" if directory else ""
    user = _user_sid(kernel, advapi)
    sddl = "D:P" + "".join(f"(A;{inherit};FA;;;{sid})" for sid in (user, "SY", "BA"))
    descriptor = ctypes.c_void_p()
    _check(advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None), "无法创建私有访问控制")
    try:
        yield kernel, advapi, descriptor
    finally:
        kernel.LocalFree(descriptor)


def assert_private_acl(path):
    """Read-only validation: protected DACL, only current user/SYSTEM/admin full access."""
    path = _local_path(path)
    kernel, advapi, _ = _windows()
    size = wintypes.DWORD()
    advapi.GetFileSecurityW(str(path), 4, None, 0, ctypes.byref(size))
    if not size.value:
        raise SecretError("无法核验私有文件访问权限。")
    descriptor = ctypes.create_string_buffer(size.value)
    _check(advapi.GetFileSecurityW(str(path), 4, descriptor, size, ctypes.byref(size)), "无法读取私有文件访问权限")
    control, revision = wintypes.WORD(), wintypes.DWORD()
    _check(advapi.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)), "无法核验访问权限继承")
    if not control.value & 0x1000:
        raise SecretError("私有访问权限仍可继承外部授权，拒绝使用。")
    present, defaulted, dacl = wintypes.BOOL(), wintypes.BOOL(), ctypes.c_void_p()
    _check(advapi.GetSecurityDescriptorDacl(descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)), "无法核验私有访问控制表")
    if not present.value or not dacl.value:
        raise SecretError("缺少私有访问控制表。")
    info = _AclSize()
    _check(advapi.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2), "无法核验私有权限条目")
    allowed = {_user_sid(kernel, advapi), "S-1-5-18", "S-1-5-32-544"}
    seen = set()
    for index in range(info.count):
        ace = ctypes.c_void_p()
        _check(advapi.GetAce(dacl, index, ctypes.byref(ace)), "无法读取私有权限条目")
        ace_type = ctypes.c_ubyte.from_address(ace.value).value
        flags = ctypes.c_ubyte.from_address(ace.value + 1).value
        mask = wintypes.DWORD.from_address(ace.value + 4).value
        if ace_type != 0 or flags & 0x18 or mask != 0x1F01FF:
            raise SecretError("私有访问权限不符合所需的明确授权。")
        sid = _sid_text(ctypes.c_void_p(ace.value + 8), kernel, advapi)
        if sid not in allowed:
            raise SecretError("私有文件仍允许其他身份访问，拒绝使用。")
        seen.add(sid)
    if seen != allowed:
        raise SecretError("私有访问权限缺少必要身份。")


def private_directory(path):
    path = _local_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path = _local_path(path)
    with _descriptor(True) as (kernel, advapi, descriptor):
        if not path.exists():
            attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
            _check(kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)), "无法创建私有目录")
        elif not path.is_dir():
            raise SecretError("私有目录路径不是目录。")
        _check(advapi.SetFileSecurityW(str(path), 4 | 0x80000000, descriptor), "无法设置私有目录访问权限")
    assert_private_acl(path)
    return path


def restrict_file(path):
    path = _local_path(path)
    if not path.is_file():
        raise SecretError("受保护路径不是普通文件。")
    with _descriptor(False) as (_, advapi, descriptor):
        _check(advapi.SetFileSecurityW(str(path), 4 | 0x80000000, descriptor), "无法设置私有文件访问权限")
    assert_private_acl(path)
    return path


def _crypt(data, purpose, decrypt):
    if not isinstance(data, bytes) or not data or len(data) > _LIMIT:
        raise SecretError("受保护数据格式或大小无效。")
    if not isinstance(purpose, str) or not purpose or len(purpose) > 100:
        raise SecretError("受保护数据用途无效。")
    kernel, _, crypt = _windows()
    source = ctypes.create_string_buffer(data, len(data))
    entropy_bytes = hashlib.sha256(("Context Relay/v1/" + purpose).encode()).digest()
    entropy = ctypes.create_string_buffer(entropy_bytes, len(entropy_bytes))
    incoming = _Blob(len(data), ctypes.cast(source, ctypes.POINTER(ctypes.c_ubyte)))
    salt = _Blob(len(entropy_bytes), ctypes.cast(entropy, ctypes.POINTER(ctypes.c_ubyte)))
    outgoing = _Blob()
    try:
        if decrypt:
            okay = crypt.CryptUnprotectData(ctypes.byref(incoming), None, ctypes.byref(salt), None, None, 1, ctypes.byref(outgoing))
        else:
            okay = crypt.CryptProtectData(ctypes.byref(incoming), "Context Relay protected secret", ctypes.byref(salt), None, None, 1, ctypes.byref(outgoing))
        _check(okay, "当前用户密钥解密或完整性核验失败" if decrypt else "当前用户密钥加密失败")
        return ctypes.string_at(outgoing.data, outgoing.size)
    finally:
        ctypes.memset(source, 0, len(data))
        if outgoing.data:
            ctypes.memset(outgoing.data, 0, outgoing.size)
            kernel.LocalFree(ctypes.cast(outgoing.data, ctypes.c_void_p))


def protect(data, purpose):
    return _MAGIC + _crypt(data, purpose, False)


def unprotect(blob, purpose):
    if not isinstance(blob, bytes) or not blob.startswith(_MAGIC):
        raise SecretError("受保护数据版本无效，不接受明文回退。")
    return _crypt(blob[len(_MAGIC):], purpose, True)


def write_private(path, data):
    """Atomic replacement; the file has its private ACL before any bytes are written."""
    path = _local_path(path)
    parent = private_directory(path.parent)
    temporary = parent / (".write-" + uuid.uuid4().hex)
    try:
        with temporary.open("xb") as stream:
            restrict_file(temporary)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            restrict_file(path)
        os.replace(temporary, path)
        assert_private_acl(path)
    finally:
        temporary.unlink(missing_ok=True)


def save(path, raw, purpose):
    write_private(path, protect(raw, purpose))
    if not hmac.compare_digest(load(path, purpose), raw):
        raise SecretError("受保护数据回读不一致。")


def load(path, purpose):
    path = _local_path(path)
    assert_private_acl(path)
    return unprotect(path.read_bytes(), purpose)


def _remove_private_tree(path, parent):
    path, parent = _local_path(path), _local_path(parent)
    if path.resolve().parent != parent.resolve() or not path.name.startswith(".tmp-"):
        raise SecretError("临时密钥清理路径不属于指定私有目录。")
    shutil.rmtree(path)


@contextmanager
def private_temporary_directory(parent):
    parent = private_directory(parent)
    directory = private_directory(parent / (".tmp-" + uuid.uuid4().hex))
    try:
        yield directory
    finally:
        try:
            _remove_private_tree(directory, parent)
        except Exception:
            raise SecretError("临时密钥清理失败；已拒绝继续，需检查私有目录内遗留文件。") from None


@contextmanager
def plaintext_file(path, purpose):
    raw = load(path, purpose)
    with private_temporary_directory(Path(path).parent) as directory:
        temporary = directory / "key.pem"
        write_private(temporary, raw)
        yield temporary

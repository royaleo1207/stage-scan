"""記住密碼：存在 Windows「認證管理員」（控制台 → 認證管理員 → Windows 認證 → 一般認證）。

- 只有勾選「記住密碼」才會儲存；取消勾選或按「忘記密碼」會刪除。
- 只有同一個 Windows 使用者登入時讀得到，不會存成一般檔案，也不會上傳。
- 非 Windows（例如測試環境）不提供記住密碼。
"""

from __future__ import annotations

import sys

TARGET_PREFIX = "stage-scan-trader:"


class SecretStore:
    """介面：available、save、load、delete。"""
    available = False

    def save(self, user: str, secret: str) -> None:
        raise RuntimeError("這台電腦不支援記住密碼")

    def load(self, user: str) -> str | None:
        return None

    def delete(self, user: str) -> None:
        pass


class MemorySecretStore(SecretStore):
    """測試用。"""
    available = True

    def __init__(self):
        self.data: dict[str, str] = {}

    def save(self, user, secret):
        self.data[user] = secret

    def load(self, user):
        return self.data.get(user)

    def delete(self, user):
        self.data.pop(user, None)


class WindowsCredentialStore(SecretStore):
    available = True

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD), ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR), ("LastWritten", wintypes.FILETIME),
                ("CredentialBlobSize", wintypes.DWORD), ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD), ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR), ("UserName", wintypes.LPWSTR),
            ]
        self.ct, self.CREDENTIAL = ctypes, CREDENTIAL
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        self.CredWrite = adv.CredWriteW
        self.CredWrite.argtypes = [ctypes.POINTER(CREDENTIAL), wintypes.DWORD]
        self.CredWrite.restype = wintypes.BOOL
        self.CredRead = adv.CredReadW
        self.CredRead.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.POINTER(ctypes.POINTER(CREDENTIAL))]
        self.CredRead.restype = wintypes.BOOL
        self.CredDelete = adv.CredDeleteW
        self.CredDelete.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD]
        self.CredDelete.restype = wintypes.BOOL
        self.CredFree = adv.CredFree
        self.CredFree.argtypes = [ctypes.c_void_p]

    CRED_TYPE_GENERIC = 1
    CRED_PERSIST_LOCAL_MACHINE = 2

    def save(self, user, secret):
        ct = self.ct
        blob = secret.encode("utf-16-le")
        buf = (ct.c_ubyte * len(blob)).from_buffer_copy(blob)
        c = self.CREDENTIAL()
        c.Type = self.CRED_TYPE_GENERIC
        c.TargetName = TARGET_PREFIX + user
        c.UserName = user
        c.Comment = "階段掃描下單程式（記住密碼）"
        c.CredentialBlobSize = len(blob)
        c.CredentialBlob = ct.cast(buf, ct.POINTER(ct.c_ubyte))
        c.Persist = self.CRED_PERSIST_LOCAL_MACHINE
        if not self.CredWrite(ct.byref(c), 0):
            raise OSError(f"無法寫入 Windows 認證管理員（錯誤 {ct.get_last_error()}）")

    def load(self, user):
        ct = self.ct
        p = ct.POINTER(self.CREDENTIAL)()
        if not self.CredRead(TARGET_PREFIX + user, self.CRED_TYPE_GENERIC, 0, ct.byref(p)):
            return None
        try:
            n = p.contents.CredentialBlobSize
            return ct.string_at(p.contents.CredentialBlob, n).decode("utf-16-le")
        finally:
            self.CredFree(p)

    def delete(self, user):
        self.CredDelete(TARGET_PREFIX + user, self.CRED_TYPE_GENERIC, 0)


def default_store() -> SecretStore:
    if sys.platform == "win32":
        try:
            return WindowsCredentialStore()
        except (OSError, AttributeError):
            pass
    return SecretStore()

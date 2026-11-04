"""以 Windows API 在記憶體解密 DPAPI 資料，並唯讀存取一般認證。"""

import ctypes
import os
from ctypes import wintypes


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


class _Credential(ctypes.Structure):
    _fields_ = [
        ("Flags", wintypes.DWORD), ("Type", wintypes.DWORD),
        ("TargetName", wintypes.LPWSTR), ("Comment", wintypes.LPWSTR),
        ("LastWritten", wintypes.FILETIME), ("CredentialBlobSize", wintypes.DWORD),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", wintypes.DWORD), ("AttributeCount", wintypes.DWORD),
        ("Attributes", ctypes.c_void_p), ("TargetAlias", wintypes.LPWSTR),
        ("UserName", wintypes.LPWSTR),
    ]


def dpapi_unprotect(data: bytes) -> bytes:
    result = None
    try:
        if os.name == "nt":
            crypt = ctypes.WinDLL("crypt32", use_last_error=True)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            crypt.CryptUnprotectData.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p, ctypes.c_void_p,
                                                ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                                                ctypes.POINTER(_Blob)]
            crypt.CryptUnprotectData.restype = wintypes.BOOL
            kernel.LocalFree.argtypes = [ctypes.c_void_p]
            kernel.LocalFree.restype = ctypes.c_void_p
            buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
            source = _Blob(len(data), buffer)
            output = _Blob()
            try:
                if crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(output)):
                    result = ctypes.string_at(output.pbData, output.cbData)
            finally:
                if output.pbData:
                    kernel.LocalFree(output.pbData)
    except Exception:
        pass
    if result is None:
        raise ValueError("無法解密 Windows 憑證") from None
    return result


def read_generic_credential(target: str) -> bytes | None:
    if os.name != "nt":
        return None
    try:
        library = ctypes.WinDLL("advapi32", use_last_error=True)
        pointer = ctypes.POINTER(_Credential)()
        library.CredReadW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                     ctypes.POINTER(ctypes.POINTER(_Credential))]
        library.CredReadW.restype = wintypes.BOOL
        library.CredFree.argtypes = [ctypes.c_void_p]
        library.CredFree.restype = None
        if not library.CredReadW(target, 1, 0, ctypes.byref(pointer)):
            return None
        try:
            credential = pointer.contents
            return ctypes.string_at(credential.CredentialBlob, credential.CredentialBlobSize)
        finally:
            library.CredFree(pointer)
    except Exception:
        return None

"""Windows private files for the Router agent: owner and DACL instead of uid and mode.

The native agent (``router-agent/src/winacl.rs``) accepts a private file only
when this user owns it (or Administrators do, as for files an elevated process
of this user creates) and no allow entry in its DACL gives anyone other than
this user, SYSTEM or Administrators more than seeing that it exists; its folder
must belong to this user (or SYSTEM/Administrators) and let no one else add,
remove or rename entries. ``is_private`` is the same rule, and ``secure``
writes what passes it: owner this user, a protected DACL for this user and
SYSTEM only.

Everything here is ctypes over advapi32/kernel32, so it imports on any
platform but works only on Windows.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import sys
from collections.abc import Iterator
from pathlib import Path

FILE = "file"
DIRECTORY = "directory"

ACCESS_ALLOWED_ACE_TYPE = 0
_DENY_ACE_TYPES = frozenset({1, 6, 10, 12})
INHERIT_ONLY_ACE = 0x08

_SYNCHRONIZE = 0x0010_0000
_READ_CONTROL = 0x0002_0000
_FILE_READ_ATTRIBUTES = 0x80
FILE_ALLOWED = _SYNCHRONIZE | _READ_CONTROL | _FILE_READ_ATTRIBUTES
DIRECTORY_FORBIDDEN = (
    0x2  # FILE_ADD_FILE
    | 0x4  # FILE_ADD_SUBDIRECTORY
    | 0x10  # FILE_WRITE_EA
    | 0x40  # FILE_DELETE_CHILD
    | 0x100  # FILE_WRITE_ATTRIBUTES
    | 0x1_0000  # DELETE
    | 0x4_0000  # WRITE_DAC
    | 0x8_0000  # WRITE_OWNER
    | 0x100_0000  # ACCESS_SYSTEM_SECURITY
    | 0x200_0000  # MAXIMUM_ALLOWED
    | 0x1000_0000  # GENERIC_ALL
    | 0x4000_0000  # GENERIC_WRITE
)

SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
OWNER_RIGHTS_SID = "S-1-3-4"


def is_private(
    kind: str,
    owner: str,
    dacl: list[tuple[int, int, int, str]] | None,
    user: str,
) -> bool:
    """The agent's rule over (ACE type, ACE flags, access mask, SID) entries.

    ``dacl`` None is a NULL DACL, which grants everyone everything.
    """
    if kind == FILE:
        owner_ok = owner in (user, ADMINISTRATORS_SID)
    else:
        owner_ok = owner in (user, SYSTEM_SID, ADMINISTRATORS_SID)
    if dacl is None or not owner_ok:
        return False
    trusted = {user, SYSTEM_SID, ADMINISTRATORS_SID, OWNER_RIGHTS_SID}
    for ace_type, flags, mask, sid in dacl:
        if flags & INHERIT_ONLY_ACE:
            continue  # applies only to children, which are checked themselves
        if ace_type == ACCESS_ALLOWED_ACE_TYPE:
            if sid in trusted:
                continue
            if kind == FILE and mask & ~FILE_ALLOWED:
                return False
            if kind == DIRECTORY and mask & DIRECTORY_FORBIDDEN:
                return False
        elif ace_type not in _DENY_ACE_TYPES:
            return False  # an allow entry this check cannot read
    return True


if sys.platform == "win32":
    import msvcrt
    from ctypes import wintypes

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _GENERIC_READ = 0x8000_0000
    _GENERIC_WRITE = 0x4000_0000
    _FILE_SHARE_ALL = 0x7
    _OPEN_EXISTING = 3
    _OPEN_ALWAYS = 4
    _FILE_FLAG_OPEN_REPARSE_POINT = 0x0020_0000
    _FILE_FLAG_BACKUP_SEMANTICS = 0x0200_0000
    _FILE_ATTRIBUTE_DIRECTORY = 0x10
    _FILE_ATTRIBUTE_REPARSE_POINT = 0x400
    _INVALID_HANDLE = wintypes.HANDLE(-1).value
    _ERROR_ALREADY_EXISTS = 183
    _SE_FILE_OBJECT = 1
    _OWNER_SECURITY_INFORMATION = 0x1
    _DACL_SECURITY_INFORMATION = 0x4
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x8000_0000
    _TOKEN_QUERY = 0x8
    _TOKEN_USER = 1

    class _ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD),
            ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME),
            ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD),
            ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD),
            ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD),
            ("nFileIndexLow", wintypes.DWORD),
        ]

    class _Acl(ctypes.Structure):
        _fields_ = [
            ("AclRevision", ctypes.c_ubyte),
            ("Sbz1", ctypes.c_ubyte),
            ("AclSize", wintypes.WORD),
            ("AceCount", wintypes.WORD),
            ("Sbz2", wintypes.WORD),
        ]

    class _AceHeader(ctypes.Structure):
        _fields_ = [
            ("AceType", ctypes.c_ubyte),
            ("AceFlags", ctypes.c_ubyte),
            ("AceSize", wintypes.WORD),
        ]

    _CreateFileW = _kernel32.CreateFileW
    _CreateFileW.restype = wintypes.HANDLE
    _CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _CloseHandle = _kernel32.CloseHandle
    _CloseHandle.argtypes = [wintypes.HANDLE]
    _GetFileInformationByHandle = _kernel32.GetFileInformationByHandle
    _GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    ]
    _LocalFree = _kernel32.LocalFree
    _LocalFree.argtypes = [ctypes.c_void_p]
    _GetCurrentProcess = _kernel32.GetCurrentProcess
    _GetCurrentProcess.restype = wintypes.HANDLE
    _GetSecurityInfo = _advapi32.GetSecurityInfo
    _GetSecurityInfo.restype = wintypes.DWORD
    _GetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _SetNamedSecurityInfoW = _advapi32.SetNamedSecurityInfoW
    _SetNamedSecurityInfoW.restype = wintypes.DWORD
    _SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    _GetAce = _advapi32.GetAce
    _GetAce.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _IsValidSid = _advapi32.IsValidSid
    _IsValidSid.argtypes = [ctypes.c_void_p]
    _ConvertSidToStringSidW = _advapi32.ConvertSidToStringSidW
    _ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.LPWSTR),
    ]
    _ConvertSD = _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    _ConvertSD.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.ULONG),
    ]
    _GetSecurityDescriptorOwner = _advapi32.GetSecurityDescriptorOwner
    _GetSecurityDescriptorOwner.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    _GetSecurityDescriptorDacl = _advapi32.GetSecurityDescriptorDacl
    _GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    _OpenProcessToken = _advapi32.OpenProcessToken
    _OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _GetTokenInformation = _advapi32.GetTokenInformation
    _GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]

    def _error() -> OSError:
        # WinError maps ERROR_FILE_NOT_FOUND to FileNotFoundError, and so on.
        return ctypes.WinError(ctypes.get_last_error())

    def _sid_string(sid: int | None) -> str | None:
        if not sid or not _IsValidSid(sid):
            return None
        text = wintypes.LPWSTR()
        if not _ConvertSidToStringSidW(sid, ctypes.byref(text)):
            return None
        try:
            return text.value
        finally:
            _LocalFree(text)

    _USER_SID: list[str] = []

    def current_user_sid() -> str:
        """This process's user SID, as a string (S-1-5-21-...)."""
        if _USER_SID:
            return _USER_SID[0]
        token = wintypes.HANDLE()
        if not _OpenProcessToken(
            _GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(token)
        ):
            raise _error()
        try:
            size = wintypes.DWORD()
            _GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
            buffer = ctypes.create_string_buffer(size.value)
            if not _GetTokenInformation(
                token, _TOKEN_USER, buffer, size, ctypes.byref(size)
            ):
                raise _error()
            # TOKEN_USER starts with a SID_AND_ATTRIBUTES, which starts with the SID pointer.
            sid = _sid_string(ctypes.c_void_p.from_buffer(buffer).value)
        finally:
            _CloseHandle(token)
        if sid is None:
            raise OSError("could not read this user's SID")
        _USER_SID.append(sid)
        return sid

    def _open(
        path: Path, kind: str, *, write: bool = False, create: bool = False
    ) -> int:
        """A handle that never follows a final symlink or junction (O_NOFOLLOW)."""
        if kind == DIRECTORY:
            access = _READ_CONTROL | _FILE_READ_ATTRIBUTES
            flags = _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS
        else:
            access = _GENERIC_READ | (_GENERIC_WRITE if write else 0)
            flags = _FILE_FLAG_OPEN_REPARSE_POINT
        handle = _CreateFileW(
            str(path),
            access,
            _FILE_SHARE_ALL,
            None,
            _OPEN_ALWAYS if create else _OPEN_EXISTING,
            flags,
            None,
        )
        if handle in (None, _INVALID_HANDLE):
            raise _error()
        return handle

    def _handle_private(handle: int, kind: str) -> bool:
        info = _ByHandleFileInformation()
        if not _GetFileInformationByHandle(handle, ctypes.byref(info)):
            return False
        attributes = info.dwFileAttributes
        if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
            return False
        if bool(attributes & _FILE_ATTRIBUTE_DIRECTORY) != (kind == DIRECTORY):
            return False
        owner, dacl, descriptor = (
            ctypes.c_void_p(),
            ctypes.c_void_p(),
            ctypes.c_void_p(),
        )
        if _GetSecurityInfo(
            handle,
            _SE_FILE_OBJECT,
            _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(descriptor),
        ):
            return False
        try:
            owner_sid = _sid_string(owner.value)
            if owner_sid is None:
                return False
            entries: list[tuple[int, int, int, str]] | None = None
            if dacl.value:
                entries = []
                acl = _Acl.from_address(dacl.value)
                for index in range(acl.AceCount):
                    ace = ctypes.c_void_p()
                    if not _GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
                        return False
                    header = _AceHeader.from_address(ace.value)
                    if header.AceType == ACCESS_ALLOWED_ACE_TYPE:
                        # ACCESS_ALLOWED_ACE: header, DWORD mask, then the SID
                        mask = wintypes.DWORD.from_address(ace.value + 4).value
                        sid = _sid_string(ace.value + 8)
                        if sid is None:
                            return False
                        entries.append((header.AceType, header.AceFlags, mask, sid))
                    else:
                        entries.append((header.AceType, header.AceFlags, 0, ""))
            return is_private(kind, owner_sid, entries, current_user_sid())
        finally:
            _LocalFree(descriptor)

    def directory_is_private(path: Path) -> bool:
        try:
            handle = _open(path, DIRECTORY)
        except OSError:
            return False
        try:
            return _handle_private(handle, DIRECTORY)
        finally:
            _CloseHandle(handle)

    def directory_owned_by_user(path: Path) -> bool:
        """Whether this user owns the folder itself (not a link to one)."""
        try:
            handle = _open(path, DIRECTORY)
        except OSError:
            return False
        try:
            info = _ByHandleFileInformation()
            if not _GetFileInformationByHandle(handle, ctypes.byref(info)):
                return False
            if info.dwFileAttributes & _FILE_ATTRIBUTE_REPARSE_POINT or not (
                info.dwFileAttributes & _FILE_ATTRIBUTE_DIRECTORY
            ):
                return False
            owner, descriptor = ctypes.c_void_p(), ctypes.c_void_p()
            if _GetSecurityInfo(
                handle,
                _SE_FILE_OBJECT,
                _OWNER_SECURITY_INFORMATION,
                ctypes.byref(owner),
                None,
                None,
                None,
                ctypes.byref(descriptor),
            ):
                return False
            try:
                return _sid_string(owner.value) == current_user_sid()
            finally:
                _LocalFree(descriptor)
        finally:
            _CloseHandle(handle)

    def secure(path: Path) -> None:
        """Make ``path`` this user's, readable only by this user and SYSTEM.

        Directories pass the same entries to the files created in them.
        """
        user = current_user_sid()
        inherit = "OICI" if path.is_dir() and not path.is_symlink() else ""
        sddl = f"O:{user}D:P(A;{inherit};FA;;;{user})(A;{inherit};FA;;;SY)"
        descriptor = ctypes.c_void_p()
        if not _ConvertSD(sddl, 1, ctypes.byref(descriptor), None):
            raise _error()
        try:
            owner, dacl = ctypes.c_void_p(), ctypes.c_void_p()
            present, defaulted = wintypes.BOOL(), wintypes.BOOL()
            if not _GetSecurityDescriptorOwner(
                descriptor, ctypes.byref(owner), ctypes.byref(defaulted)
            ) or not _GetSecurityDescriptorDacl(
                descriptor,
                ctypes.byref(present),
                ctypes.byref(dacl),
                ctypes.byref(defaulted),
            ):
                raise _error()
            result = _SetNamedSecurityInfoW(
                str(path),
                _SE_FILE_OBJECT,
                _OWNER_SECURITY_INFORMATION
                | _DACL_SECURITY_INFORMATION
                | _PROTECTED_DACL_SECURITY_INFORMATION,
                owner,
                None,
                dacl,
                None,
            )
            if result:
                raise ctypes.WinError(result)
        finally:
            _LocalFree(descriptor)

    @contextlib.contextmanager
    def open_private(
        path: Path, *, write: bool = False, create: bool = False
    ) -> Iterator[int]:
        """A file descriptor for a private regular file (created private if missing)."""
        handle = _open(path, FILE, write=write, create=create)
        try:
            if create and ctypes.get_last_error() != _ERROR_ALREADY_EXISTS:
                secure(path)
            if not _handle_private(handle, FILE):
                raise OSError("Router agent file is not private to this user")
            fd = msvcrt.open_osfhandle(handle, os.O_RDWR if write else os.O_RDONLY)
        except BaseException:
            _CloseHandle(handle)
            raise
        try:
            yield fd
        finally:
            os.close(fd)

else:

    def directory_owned_by_user(path: Path) -> bool:
        raise OSError("Windows only")

    def current_user_sid() -> str:
        raise OSError("Windows only")

    def directory_is_private(path: Path) -> bool:
        raise OSError("Windows only")

    def secure(path: Path) -> None:
        raise OSError("Windows only")

    def open_private(
        path: Path, *, write: bool = False, create: bool = False
    ) -> Iterator[int]:
        raise OSError("Windows only")

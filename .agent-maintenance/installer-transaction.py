#!/usr/bin/env python3
"""Internal file-operation protocol for install.sh, not a standalone installer.

The shell entry owns interactive authorization; these operations (like copy and
rollback) assume a trusted caller already authorized the requested local write.
They enforce filesystem safety, not a security boundary against the local user.
"""

import base64
import ctypes
import errno
import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import time


class InstallError(Exception):
    pass


DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
NOFOLLOW = getattr(os, "O_NOFOLLOW", None)
if NOFOLLOW is None:
    raise SystemExit("ERROR: this platform cannot safely reject symbolic-link paths.")


def fail(message, status=3):
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(status)


def open_root(root):
    root = os.path.abspath(root)
    fd = os.open("/", DIR_FLAGS)
    try:
        for component in (part for part in root.split("/") if part):
            next_fd = os.open(component, DIR_FLAGS | NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except Exception:
        os.close(fd)
        raise


def load_state(filename):
    try:
        with open(filename, encoding="utf-8") as handle:
            state = json.load(handle)
    except FileNotFoundError:
        state = {"operations": [], "created_directories": []}
    if not isinstance(state, dict):
        raise InstallError("transaction journal is not a JSON object")
    state.setdefault("operations", [])
    state.setdefault("created_directories", [])
    return state


def save_state(filename, state):
    temporary = f"{filename}.tmp-{os.getpid()}-{secrets.token_hex(4)}"
    try:
        with open(temporary, "x", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, filename)
    except Exception:
        # The previously published journal can omit a just-created directory or
        # pending publication. Preserve the in-memory state independently of the
        # failed replace, and keep the original failure visible to the caller.
        recovery = f"{filename}.pending-{os.getpid()}-{secrets.token_hex(6)}"
        recovery_fd = None
        try:
            recovery_fd = os.open(recovery, os.O_WRONLY | os.O_CREAT | os.O_EXCL | NOFOLLOW, 0o600)
            payload = (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            view = memoryview(payload)
            while view:
                written = os.write(recovery_fd, view)
                if written <= 0:
                    raise OSError("short emergency journal write")
                view = view[written:]
            os.fsync(recovery_fd)
            print(f"PRESERVED pending transaction journal for manual recovery: {recovery}", file=sys.stderr)
        except Exception as recovery_error:
            print(f"ERROR: pending transaction journal could not be persisted: {recovery_error}; keep all reported recovery entries and the private work directory", file=sys.stderr)
        finally:
            if recovery_fd is not None:
                os.close(recovery_fd)
        raise
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def split_relative(relative):
    if not relative or relative.startswith("/"):
        raise InstallError(f"unsafe target path: {relative!r}")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise InstallError(f"unsafe target path: {relative!r}")
    return parts


def open_parent(root_fd, relative, create, state, state_path):
    parts = split_relative(relative)
    fd = os.dup(root_fd)
    walked = []
    try:
        for component in parts[:-1]:
            walked.append(component)
            try:
                next_fd = os.open(component, DIR_FLAGS | NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, 0o777, dir_fd=fd)
                state["created_directories"].append("/".join(walked))
                try:
                    save_state(state_path, state)
                except Exception:
                    print(
                        f"PRESERVED directory created before transaction journal failure; rollback was incomplete because it was not removed safely: {'/'.join(walked)}",
                        file=sys.stderr,
                    )
                    raise
                next_fd = os.open(component, DIR_FLAGS | NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd, parts[-1]
    except Exception:
        os.close(fd)
        raise


def verify_parent(root, root_fd, relative, parent_fd):
    """Re-resolve the project path and ensure the held parent is still beneath it."""
    current_fd = None
    parent_check_fd = None
    try:
        current_fd = open_root(root)
        root_info = os.fstat(root_fd)
        current_root_info = os.fstat(current_fd)
        if (root_info.st_dev, root_info.st_ino) != (current_root_info.st_dev, current_root_info.st_ino):
            return False
        parent_check_fd = os.dup(current_fd)
        for component in split_relative(relative)[:-1]:
            next_fd = os.open(component, DIR_FLAGS | NOFOLLOW, dir_fd=parent_check_fd)
            os.close(parent_check_fd)
            parent_check_fd = next_fd
        expected = os.fstat(parent_fd)
        actual = os.fstat(parent_check_fd)
        return (expected.st_dev, expected.st_ino) == (actual.st_dev, actual.st_ino)
    except (OSError, InstallError):
        return False
    finally:
        if parent_check_fd is not None:
            os.close(parent_check_fd)
        if current_fd is not None:
            os.close(current_fd)


def rename_noreplace(parent_fd, source, destination):
    """Move an entry only if the recovery name is still absent; fail closed if unavailable."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function_name, flag = "renameatx_np", 0x00000004  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        function_name, flag = "renameat2", 0x00000001  # RENAME_NOREPLACE
    else:
        raise InstallError("this platform has no supported atomic no-replace rename for rollback")
    try:
        function = getattr(libc, function_name)
    except AttributeError as exc:
        raise InstallError("atomic no-replace rename is unavailable; rollback left the file in place") from exc
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    result = function(parent_fd, os.fsencode(source), parent_fd, os.fsencode(destination), flag)
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), destination)


_DARWIN_XATTR_SHOWCOMPRESSION = 0x20


def _darwin_xattr_functions():
    if sys.platform != "darwin":
        return None
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        functions = {
            "list": libc.flistxattr,
            "get": libc.fgetxattr,
            "set": libc.fsetxattr,
            "remove": libc.fremovexattr,
        }
    except AttributeError:
        return None
    functions["list"].argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
    functions["list"].restype = ctypes.c_ssize_t
    functions["get"].argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    functions["get"].restype = ctypes.c_ssize_t
    functions["set"].argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int]
    functions["set"].restype = ctypes.c_int
    functions["remove"].argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    functions["remove"].restype = ctypes.c_int
    return functions


def _xattr_api():
    # Darwin's default os API cannot request SHOWCOMPRESSION. Always use the
    # fd ABI there so the visible attribute set does not depend on Python's
    # build, and hidden compression metadata is never silently discarded.
    if sys.platform == "darwin":
        fallback = _darwin_xattr_functions()
        if fallback is None:
            raise OSError(errno.ENOTSUP, "native descriptor xattr APIs are unavailable")
        return ("darwin", fallback)
    native = {
        "list": getattr(os, "listxattr", None),
        "get": getattr(os, "getxattr", None),
        "set": getattr(os, "setxattr", None),
        "remove": getattr(os, "removexattr", None),
    }
    if all(callable(function) for function in native.values()):
        return ("native", native)
    return None


def _xattr_name_bytes(name):
    if not isinstance(name, str) or not name or "\x00" in name:
        raise OSError(errno.EINVAL, "invalid extended attribute name")
    return os.fsencode(name)


def _darwin_list(function, fd):
    size = function(fd, None, 0, _DARWIN_XATTR_SHOWCOMPRESSION)
    if size < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    buffer = ctypes.create_string_buffer(size) if size else None
    result = function(fd, buffer, size, _DARWIN_XATTR_SHOWCOMPRESSION)
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    if result != size:
        raise OSError(errno.ERANGE, "extended attribute name list changed during inspection")
    if size == 0:
        return []
    raw = buffer.raw[:result]
    if not raw or raw[-1] != 0:
        raise OSError(errno.EIO, "malformed extended attribute name list")
    names = raw[:-1].split(b"\x00")
    if any(not name for name in names) or len(set(names)) != len(names):
        raise OSError(errno.EIO, "malformed extended attribute name list")
    try:
        return [name.decode("utf-8") for name in names]
    except UnicodeDecodeError as exc:
        raise OSError(errno.EILSEQ, "invalid extended attribute name encoding") from exc


def _darwin_get(function, fd, name):
    encoded = _xattr_name_bytes(name)
    size = function(fd, encoded, None, 0, 0, _DARWIN_XATTR_SHOWCOMPRESSION)
    if size < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), name)
    buffer = ctypes.create_string_buffer(size) if size else None
    result = function(fd, encoded, buffer, size, 0, _DARWIN_XATTR_SHOWCOMPRESSION)
    if result < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), name)
    if result != size:
        raise OSError(errno.ERANGE, "extended attribute changed during inspection", name)
    return b"" if result == 0 else buffer.raw[:result]


def xattr_list(fd):
    api = _xattr_api()
    if api is None:
        raise NotImplementedError("xattr APIs are unavailable")
    kind, functions = api
    if kind == "native":
        names = list(functions["list"](fd))
        if (any(not isinstance(name, str) or not name or "\x00" in name for name in names)
                or len(set(names)) != len(names)):
            raise OSError(errno.EIO, "malformed extended attribute name list")
        return names
    return _darwin_list(functions["list"], fd)


def xattr_get(fd, name):
    api = _xattr_api()
    if api is None:
        raise NotImplementedError("xattr APIs are unavailable")
    kind, functions = api
    if kind == "native":
        return functions["get"](fd, name)
    return _darwin_get(functions["get"], fd, name)


def xattr_set(fd, name, value):
    api = _xattr_api()
    if api is None:
        raise NotImplementedError("xattr APIs are unavailable")
    if not isinstance(value, bytes):
        raise TypeError("extended attribute value must be bytes")
    kind, functions = api
    if kind == "native":
        return functions["set"](fd, name, value)
    encoded = _xattr_name_bytes(name)
    buffer = ctypes.create_string_buffer(value) if value else None
    result = functions["set"](fd, encoded, buffer, len(value), 0, 0)
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), name)


def xattr_remove(fd, name):
    api = _xattr_api()
    if api is None:
        raise NotImplementedError("xattr APIs are unavailable")
    kind, functions = api
    if kind == "native":
        return functions["remove"](fd, name)
    result = functions["remove"](fd, _xattr_name_bytes(name), 0)
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), name)


def _darwin_acl_functions():
    if sys.platform != "darwin":
        return None
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        functions = {name: getattr(libc, name) for name in (
            "acl_get_fd_np", "acl_set_fd_np", "acl_free", "acl_size",
            "acl_copy_ext_native", "acl_copy_int_native")}
    except AttributeError:
        return None
    functions["acl_get_fd_np"].argtypes = [ctypes.c_int, ctypes.c_int]
    functions["acl_get_fd_np"].restype = ctypes.c_void_p
    functions["acl_set_fd_np"].argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    functions["acl_set_fd_np"].restype = ctypes.c_int
    functions["acl_free"].argtypes = [ctypes.c_void_p]
    functions["acl_free"].restype = ctypes.c_int
    functions["acl_size"].argtypes = [ctypes.c_void_p]
    functions["acl_size"].restype = ctypes.c_ssize_t
    functions["acl_copy_ext_native"].argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t]
    functions["acl_copy_ext_native"].restype = ctypes.c_ssize_t
    functions["acl_copy_int_native"].argtypes = [ctypes.c_void_p]
    functions["acl_copy_int_native"].restype = ctypes.c_void_p
    return functions


_DARWIN_ACL_TYPE_EXTENDED = 0x00000100


def _darwin_acl(fd):
    functions = _darwin_acl_functions()
    if functions is None:
        if sys.platform == "darwin":
            raise OSError(errno.ENOTSUP, "Darwin ACL APIs unavailable")
        return {"supported": False, "present": False, "value": None}
    ctypes.set_errno(0)
    acl = functions["acl_get_fd_np"](fd, _DARWIN_ACL_TYPE_EXTENDED)
    if not acl:
        error = ctypes.get_errno()
        if error == errno.ENOENT:
            return {"supported": True, "present": False, "value": None}
        raise OSError(error or errno.EIO, os.strerror(error or errno.EIO), "acl_get_fd_np")
    try:
        size = functions["acl_size"](acl)
        if size <= 0:
            raise OSError(errno.EIO, "invalid Darwin ACL size")
        buffer = ctypes.create_string_buffer(size)
        copied = functions["acl_copy_ext_native"](buffer, acl, size)
        if copied != size:
            raise OSError(errno.EIO, "Darwin ACL serialization size mismatch")
        return {"supported": True, "present": True, "value": base64.b64encode(buffer.raw[:size]).decode("ascii")}
    finally:
        functions["acl_free"](acl)


def _darwin_set_acl(fd, value):
    if sys.platform != "darwin":
        if value.get("supported") or value.get("present"):
            raise OSError(errno.ENOTSUP, "cannot apply Darwin ACL on this platform")
        return
    functions = _darwin_acl_functions()
    if functions is None:
        raise OSError(errno.ENOTSUP, "Darwin ACL APIs unavailable")
    if not value.get("present"):
        # acl_set_fd_np cannot portably encode absence; verify absence instead.
        actual = _darwin_acl(fd)
        if actual["present"]:
            raise OSError(errno.EPERM, "cannot safely remove existing Darwin ACL")
        return
    raw = base64.b64decode(value["value"], validate=True)
    # acl_copy_int_native has no length argument. Validate the kauth_filesec
    # header and complete ACE area before handing the pointer to libc.
    import struct
    if len(raw) < 44 or len(raw) > 1024 * 1024 or len(raw) % 4:
        raise OSError(errno.EINVAL, "invalid Darwin ACL serialization length")
    magic = struct.unpack_from("=I", raw, 0)[0]
    entry_count = struct.unpack_from("=I", raw, 36)[0]
    if magic != 0x012CC16D or entry_count > 128:
        raise OSError(errno.EINVAL, "invalid Darwin ACL serialization header")
    # kauth_filesec contains magic, owner/group GUIDs, and kauth_acl header;
    # each kauth_ace is 24 bytes.
    expected = 44 + entry_count * 24
    if len(raw) != expected:
        raise OSError(errno.EINVAL, "invalid Darwin ACL serialization entry area")
    buffer = ctypes.create_string_buffer(raw)
    acl = functions["acl_copy_int_native"](buffer)
    if not acl:
        error = ctypes.get_errno()
        raise OSError(error or errno.EIO, os.strerror(error or errno.EIO), "acl_copy_int_native")
    try:
        if functions["acl_set_fd_np"](fd, acl, _DARWIN_ACL_TYPE_EXTENDED) != 0:
            error = ctypes.get_errno()
            raise OSError(error or errno.EIO, os.strerror(error or errno.EIO), "acl_set_fd_np")
    finally:
        functions["acl_free"](acl)


def _file_flags(fd):
    info = os.fstat(fd)
    value = getattr(info, "st_flags", None)
    if value is None:
        if sys.platform == "darwin":
            raise OSError(errno.ENOTSUP, "Darwin st_flags unavailable")
        return {"supported": False, "value": None}
    return {"supported": True, "value": int(value)}


def _set_file_flags(fd, flags):
    if not flags.get("supported"):
        return
    # Never mask immutable/append/nounlink/unknown flags or leave staging with
    # flags that prevent a safe exchange or recovery.
    if flags["value"] & ~(0x00000001 | 0x00008000):  # UF_NODUMP | UF_HIDDEN
        raise OSError(errno.ENOTSUP, "cannot safely stage these BSD file flags")
    function = getattr(ctypes.CDLL(None, use_errno=True), "fchflags", None)
    if function is None:
        raise OSError(errno.ENOTSUP, "fchflags unavailable")
    function.argtypes = [ctypes.c_int, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(fd, flags["value"]) != 0:
        error = ctypes.get_errno()
        raise OSError(error or errno.EIO, os.strerror(error or errno.EIO), "fchflags")


def protected_metadata(fd):
    return {"acl": _darwin_acl(fd), "flags": _file_flags(fd)}


def metadata_matches(left, right):
    return left == right


def xattrs(fd):
    if _xattr_api() is None:
        return None
    before = os.fstat(fd)
    names = xattr_list(fd)
    if sys.platform == "darwin" and "com.apple.decmpfs" in names:
        # These bytes describe the *old compressed contents*. Copying them onto
        # a new expanded payload is not a safe metadata-preserving update.
        raise InstallError("cannot safely rewrite compression metadata; original file preserved")
    values = {}
    for name in names:
        values[name] = base64.b64encode(xattr_get(fd, name)).decode("ascii")
    final_names = xattr_list(fd)
    verified = {
        name: base64.b64encode(xattr_get(fd, name)).decode("ascii")
        for name in final_names
    }
    after = os.fstat(fd)
    stable = ("st_dev", "st_ino", "st_size", "st_mode", "st_uid", "st_gid",
              "st_nlink", "st_mtime_ns", "st_ctime_ns")
    if (values != verified
            or any(getattr(before, field) != getattr(after, field) for field in stable)):
        raise InstallError("file or extended attributes changed during inspection")
    return values


def identity(fd):
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        raise InstallError("destination is not a regular file")
    os.lseek(fd, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
    after = os.fstat(fd)
    stable = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode", "st_uid", "st_gid",
              "st_nlink", "st_ctime_ns")
    if any(getattr(before, name) != getattr(after, name) for name in stable):
        raise InstallError("file changed while it was being inspected")
    attributes = xattrs(fd)
    protected = protected_metadata(fd)
    verified_protected = protected_metadata(fd)
    final = os.fstat(fd)
    if any(getattr(after, name) != getattr(final, name) for name in stable) or protected != verified_protected:
        raise InstallError("file changed while its extended attributes or protected metadata were inspected")
    return {
        "device": after.st_dev,
        "inode": after.st_ino,
        "size": after.st_size,
        "nlink": after.st_nlink,
        "mtime_ns": after.st_mtime_ns,
        "ctime_ns": after.st_ctime_ns,
        "mode": stat.S_IMODE(after.st_mode),
        "uid": after.st_uid,
        "gid": after.st_gid,
        "sha256": digest.hexdigest(),
        "xattrs": None if attributes is None else hashlib.sha256(
            json.dumps(attributes, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "protected": protected,
    }


def same_identity(left, right):
    # Without xattr inspection, a matching inode/content signature is not
    # enough evidence to remove a recovery candidate during rollback.
    if not left or not right or left.get("xattrs") is None or right.get("xattrs") is None:
        return False
    return all(left.get(key) == right.get(key) for key in (
        "device", "inode", "size", "nlink", "mtime_ns", "ctime_ns", "mode", "uid", "gid", "sha256", "xattrs", "protected"
    ))


def namespace_identity_matches(actual, before):
    """Validate only the immediately observed installer-owned link/rename.

    This is not an authorization check for a later read or rollback: callers
    must persist the exact resulting ctime and use strict identity thereafter.
    """
    return bool(actual and before) and actual == {**before, "ctime_ns": actual["ctime_ns"]}


def record_published_identity(root, root_fd, relative, parent_fd, name,
                              temp_fd, expected, operation, state, state_path):
    """Record a new installer-owned inode after its link/unlink publication.

    A failed observation or journal write leaves the pre-publication version
    in the durable journal. Strict rollback then preserves uncertain data.
    Never use this to reauthorize pre-existing files or stale journal entries.
    """
    published = identity(temp_fd)
    if not namespace_identity_matches(published, expected):
        raise InstallError(f"new file changed during publication; preserved: {relative}")
    active = signature_at(parent_fd, name)
    if active != published or not verify_parent(root, root_fd, relative, parent_fd):
        raise InstallError(f"published pathname or parent changed; preserved: {relative}")
    operation["expected"] = published
    operation["status"] = "published"
    save_state(state_path, state)


def read_open_file(fd):
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode):
        raise InstallError("settings path is not a regular file")
    before_protected = protected_metadata(fd)
    before_xattrs = xattrs(fd)
    os.lseek(fd, 0, os.SEEK_SET)
    chunks = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            break
        chunks.append(chunk)
    after_protected = protected_metadata(fd)
    after_xattrs = xattrs(fd)
    after = os.fstat(fd)
    stable = ("st_dev", "st_ino", "st_size", "st_mode", "st_uid", "st_gid", "st_nlink", "st_mtime_ns", "st_ctime_ns")
    if (any(getattr(before, field) != getattr(after, field) for field in stable)
            or before_protected != after_protected or before_xattrs != after_xattrs):
        raise InstallError("file or protected metadata changed while it was being read")
    return b"".join(chunks)


def open_existing(parent_fd, name, reject_hardlinks=True):
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise InstallError("destination is a symbolic link")
    if not stat.S_ISREG(info.st_mode):
        raise InstallError("destination is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
    noatime = getattr(os, "O_NOATIME", 0)
    try:
        fd = os.open(name, flags | noatime, dir_fd=parent_fd)
    except OSError as exc:
        if not noatime or exc.errno not in {errno.EPERM, errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTSUP}:
            raise
        fd = os.open(name, flags, dir_fd=parent_fd)
    if reject_hardlinks and os.fstat(fd).st_nlink > 1:
        os.close(fd)
        raise InstallError("destination has multiple hard links; refusing to modify it")
    return fd


def inspect_existing(parent_fd, name, allow_hardlinks=False):
    fd = open_existing(parent_fd, name, reject_hardlinks=not allow_hardlinks)
    if fd is None:
        return None
    try:
        data = read_open_file(fd)
        return {"data": data, "nlink": os.fstat(fd).st_nlink}
    finally:
        os.close(fd)


def apply_metadata(fd, values):
    try:
        wanted_protected = values.get("protected")
        if wanted_protected is None:
            # New files inherit the directory's security policy, not an
            # assumed empty ACL. Replacements always provide a full baseline.
            wanted_protected = protected_metadata(fd)
        if (os.fstat(fd).st_uid, os.fstat(fd).st_gid) != (values["uid"], values["gid"]):
            os.fchown(fd, values["uid"], values["gid"])
        os.fchmod(fd, values["mode"])
        wanted = {
            name: base64.b64decode(value)
            for name, value in values.get("xattrs", {}).items()
        }
        _set_file_flags(fd, wanted_protected["flags"])
        if _xattr_api() is not None:
            for name in xattr_list(fd):
                if name not in wanted:
                    xattr_remove(fd, name)
            for name, value in wanted.items():
                xattr_set(fd, name, value)
            actual = xattrs(fd)
        elif wanted:
            raise NotImplementedError("xattr APIs are unavailable for requested file attributes")
        else:
            # A new transaction-owned staging file requests no xattrs; core
            # mode/owner/time metadata can still be applied safely.
            actual = None
        os.utime(fd, ns=(os.fstat(fd).st_atime_ns, values["mtime_ns"]))
        _set_file_flags(fd, wanted_protected["flags"])
        _darwin_set_acl(fd, wanted_protected["acl"])
        info = os.fstat(fd)
        if (
            (info.st_uid, info.st_gid) != (values["uid"], values["gid"])
            or stat.S_IMODE(info.st_mode) != values["mode"]
            or info.st_mtime_ns != values["mtime_ns"]
            or (actual is not None and actual != values.get("xattrs", {}))
            or protected_metadata(fd) != wanted_protected
        ):
            raise OSError("staged file metadata did not match the original")
    except (OSError, AttributeError, NotImplementedError) as exc:
        raise InstallError(f"could not safely preserve file metadata: {exc}") from exc


def new_temp(parent_fd, name, data, values, temporary_name=None, preserve_on_failure=False):
    temporary = temporary_name or f".{name}.flywheel-{os.getpid()}-{secrets.token_hex(6)}"
    # Confirmation snapshots inspect this same descriptor after staging. On an
    # atime-enabled filesystem, a read/utime restore changes ctime itself and
    # would look like an external write. The newly created inode belongs to us,
    # so Linux O_NOATIME needs no privilege escalation. Other paths are unchanged.
    noatime = getattr(os, "O_NOATIME", 0) if preserve_on_failure else 0
    fd = os.open(
        temporary,
        os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | NOFOLLOW | noatime,
        0o600,
        dir_fd=parent_fd,
    )
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        apply_metadata(fd, values)
        os.fsync(fd)
        expected = identity(fd)
        return temporary, fd, expected
    except Exception:
        os.close(fd)
        if preserve_on_failure:
            print(f"PRESERVED confirmation staging/recovery entry: {temporary}", file=sys.stderr)
        else:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except OSError:
                pass
        raise


def source_file(path):
    fd = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0) | NOFOLLOW,
    )
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise InstallError(f"source is not a regular file: {path}")
        data = read_open_file(fd)
        values = {
            "mode": stat.S_IMODE(info.st_mode),
            "uid": info.st_uid,
            "gid": info.st_gid,
            "mtime_ns": info.st_mtime_ns,
            "xattrs": {},
            "protected": protected_metadata(fd),
        }
        return data, values
    finally:
        os.close(fd)


def compare_command(root, relative, source):
    """Compare regular files through pinned descriptors without FIFO blocking."""
    source_fd = None
    root_fd = open_root(root)
    parent_fd = None
    destination_fd = None
    try:
        source_fd = os.open(
            source,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0) | NOFOLLOW,
        )
        source_info = os.fstat(source_fd)
        if not stat.S_ISREG(source_info.st_mode):
            raise InstallError(f"source is not a regular file: {source}")
        parent_fd, name = open_parent(root_fd, relative, False, {"created_directories": []}, "")
        destination_fd = open_existing(parent_fd, name, reject_hardlinks=False)
        if destination_fd is None:
            return 1
        destination_info = os.fstat(destination_fd)
        if not stat.S_ISREG(destination_info.st_mode):
            raise InstallError(f"destination is not a regular file: {relative}")
        os.lseek(source_fd, 0, os.SEEK_SET)
        os.lseek(destination_fd, 0, os.SEEK_SET)
        while True:
            source_chunk = os.read(source_fd, 1024 * 1024)
            destination_chunk = os.read(destination_fd, 1024 * 1024)
            if source_chunk != destination_chunk:
                return 1
            if not source_chunk:
                break
        if (os.fstat(source_fd).st_size != source_info.st_size or
                os.fstat(destination_fd).st_size != destination_info.st_size):
            raise InstallError(f"file changed while being compared: {relative}")
        return 0
    finally:
        if destination_fd is not None:
            os.close(destination_fd)
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(root_fd)
        if source_fd is not None:
            os.close(source_fd)


def path_exists(parent_fd, name):
    try:
        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def op_copy(root, relative, source, state_path):
    state = load_state(state_path)
    root_fd = open_root(root)
    parent_fd = None
    temporary = None
    temp_fd = None
    try:
        parent_fd, name = open_parent(root_fd, relative, True, state, state_path)
        if not verify_parent(root, root_fd, relative, parent_fd):
            raise InstallError(f"target directory moved or changed after it was opened; refusing write: {relative}")
        if path_exists(parent_fd, name):
            print("EXISTS")
            raise SystemExit(10)
        data, values = source_file(source)
        if not verify_parent(root, root_fd, relative, parent_fd):
            raise InstallError(f"target directory moved or changed before staging: {relative}")
        temporary, temp_fd, expected = new_temp(parent_fd, name, data, values)
        operation = {"path": relative, "expected": expected}
        state["operations"].append(operation)
        save_state(state_path, state)
        if not verify_parent(root, root_fd, relative, parent_fd):
            raise InstallError(f"target directory moved or changed before publishing; staged file was not installed: {relative}")
        if identity(temp_fd) != expected:
            raise InstallError(f"new staging inode changed before publishing: {relative}")
        try:
            os.link(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
        except FileExistsError:
            state["operations"].pop()
            save_state(state_path, state)
            print("EXISTS")
            raise SystemExit(10)
        if not verify_parent(root, root_fd, relative, parent_fd):
            print(f"PRESERVED file written through a directory moved during publication: {relative}", file=sys.stderr)
            raise InstallError(f"target directory moved during publication; rollback will preserve uncertain data: {relative}")
        linked = signature_at(parent_fd, name, reject_hardlinks=False)
        if (linked != identity(temp_fd)
                or not namespace_identity_matches(linked, {**expected, "nlink": expected["nlink"] + 1})):
            raise InstallError(f"new file changed at link publication; preserved: {relative}")
        os.unlink(temporary, dir_fd=parent_fd)
        temporary = None
        record_published_identity(root, root_fd, relative, parent_fd, name,
                                  temp_fd, expected, operation, state, state_path)
        print("INSTALLED")
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        if temporary is not None and parent_fd is not None:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except OSError:
                pass
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(root_fd)


def validate_config(config, filename):
    if not isinstance(config, dict):
        raise InstallError(f"{filename} must contain a JSON object")
    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise InstallError(f"{filename}: 'hooks' must be a JSON object")
    sessions = hooks.setdefault("SessionStart", [])
    if not isinstance(sessions, list):
        raise InstallError(f"{filename}: 'hooks.SessionStart' must be a JSON array")
    for session in sessions:
        if not isinstance(session, dict):
            raise InstallError(f"{filename}: each SessionStart entry must be a JSON object")
        if "matcher" in session and not isinstance(session["matcher"], str):
            raise InstallError(f"{filename}: each SessionStart matcher must be a string")
        handlers = session.get("hooks")
        if not isinstance(handlers, list):
            raise InstallError(f"{filename}: each SessionStart 'hooks' value must be a JSON array")
        for handler in handlers:
            if not isinstance(handler, dict) or not isinstance(handler.get("type"), str):
                raise InstallError(f"{filename}: each SessionStart hook must be an object with a string 'type'")
            if handler["type"] == "command" and not isinstance(handler.get("command"), str):
                raise InstallError(f"{filename}: each command SessionStart hook must have a string 'command'")
    return sessions


def hook_entry(provider):
    if provider == "codex":
        return {
            "matcher": "startup|resume|clear|compact|fork",
            "hooks": [{
                "type": "command",
                "command": 'bash "$(git rev-parse --show-toplevel)/.agent-maintenance/session-start.sh" codex',
                "timeout": 5,
                "additionalContextLimit": 1200,
            }],
        }
    return {
        "matcher": "startup|resume|clear|compact|fork",
        "hooks": [{
            "type": "command",
            "command": 'bash "${CLAUDE_PROJECT_DIR}/.agent-maintenance/session-start.sh" claude',
        }],
    }


def hook_matches_all_session_sources(session):
    matcher = session.get("matcher")
    return "matcher" not in session or matcher in (
        "",
        "*",
        ".*",
        "startup|resume|clear|compact|fork",
    )


def hook_is_registered(provider, sessions):
    command = hook_entry(provider)["hooks"][0]["command"]
    return any(
        isinstance(session, dict)
        and hook_matches_all_session_sources(session)
        and any(
            isinstance(handler, dict)
            and handler.get("type") == "command"
            and handler.get("command") == command
            for handler in session.get("hooks", [])
        )
        for session in sessions
    )


LOCK_FD_ENV = "SELF_EVOLUTION_FLYWHEEL_LOCK_FD"
HOOK_SETTINGS = {
    "codex": ".codex/hooks.json",
    "claude": ".claude/settings.json",
}


def acquire_target_lock(root):
    fd = open_root(root)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        os.close(fd)
        raise InstallError("another installer is active for this target; retry after it exits") from exc
    except OSError as exc:
        os.close(fd)
        raise InstallError(f"cannot acquire an exclusive target lock: {exc}") from exc
    return fd


def verify_target_lock(root, descriptor):
    try:
        fd = int(descriptor)
        held = os.fstat(fd)
        if not stat.S_ISDIR(held.st_mode):
            raise InstallError("installer lock descriptor is not a directory")
        root_fd = open_root(root)
        try:
            actual = os.fstat(root_fd)
        finally:
            os.close(root_fd)
        if (held.st_dev, held.st_ino) != (actual.st_dev, actual.st_ino):
            raise InstallError("installer lock descriptor does not match the target directory")
        # Reasserting flock on the inherited open-file description is safe; if
        # this descriptor was not locked by the launcher, it locks it here.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except InstallError:
        raise
    except (OSError, ValueError) as exc:
        raise InstallError(f"installer target lock is missing or invalid: {exc}") from exc


def lock_and_exec(root, command):
    if not command:
        raise InstallError("target lock launcher requires a command")
    fd = acquire_target_lock(root)
    os.set_inheritable(fd, True)
    env = os.environ.copy()
    env[LOCK_FD_ENV] = str(fd)
    os.execvpe(command[0], command, env)


def expected_new_hook_payload(provider):
    config = {}
    sessions = validate_config(config, HOOK_SETTINGS[provider])
    sessions.append(hook_entry(provider))
    return (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def recover_provider_staging(root, provider, report=True):
    if provider not in HOOK_SETTINGS:
        raise InstallError(f"unknown provider: {provider}")
    descriptor = os.environ.get(LOCK_FD_ENV)
    if descriptor is None:
        raise InstallError("refusing staging recovery without the exclusive target lock")
    verify_target_lock(root, descriptor)

    relative = HOOK_SETTINGS[provider]
    state = {"operations": [], "created_directories": []}
    root_fd = open_root(root)
    parent_fd = None
    try:
        try:
            parent_fd, name = open_parent(root_fd, relative, False, state, "")
        except FileNotFoundError:
            return False
        if not verify_parent(root, root_fd, relative, parent_fd):
            raise InstallError(f"target directory moved or changed during staging recovery: {relative}")

        prefix = f".{name}.flywheel-"
        matching = [entry for entry in os.listdir(parent_fd) if entry.startswith(prefix)]
        if not matching:
            return False
        pattern = re.compile(rf"\.{re.escape(name)}\.flywheel-[0-9]+-[0-9a-f]{{12}}\Z")
        unknown = [entry for entry in matching if pattern.fullmatch(entry) is None]
        if unknown:
            raise InstallError(
                f"unrecognized possible provider staging entry for {relative}: {unknown[0]}; left untouched"
            )
        if len(matching) != 1:
            raise InstallError(f"multiple possible provider staging entries for {relative}; left untouched")
        alias = matching[0]

        try:
            active_info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise InstallError(f"staging entry {alias} has no unique active settings file {relative}; left untouched") from exc
        alias_info = os.stat(alias, dir_fd=parent_fd, follow_symlinks=False)
        if not stat.S_ISREG(active_info.st_mode) or not stat.S_ISREG(alias_info.st_mode):
            raise InstallError(f"possible provider staging entry {alias} is not a regular file; left untouched")
        if (active_info.st_dev, active_info.st_ino) != (alias_info.st_dev, alias_info.st_ino):
            raise InstallError(f"possible provider staging entry {alias} is not the active settings inode; left untouched")
        if active_info.st_nlink != 2 or alias_info.st_nlink != 2:
            raise InstallError(
                f"possible provider staging entry {alias} has link count {active_info.st_nlink}, not exactly 2; left untouched"
            )
        expected_owner = (os.geteuid(), os.getegid())
        if (active_info.st_uid, active_info.st_gid) != expected_owner or (
            alias_info.st_uid,
            alias_info.st_gid,
        ) != expected_owner:
            raise InstallError(f"possible provider staging entry {alias} has an unexpected owner/group; left untouched")
        expected_mode = 0o644
        if stat.S_IMODE(active_info.st_mode) != expected_mode or stat.S_IMODE(alias_info.st_mode) != expected_mode:
            raise InstallError(f"possible provider staging entry {alias} has unexpected permissions; left untouched")

        fd = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | NOFOLLOW | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino, opened.st_nlink) != (
                active_info.st_dev,
                active_info.st_ino,
                2,
            ):
                raise InstallError(f"active settings changed while validating staging entry {alias}; left untouched")
            data = read_open_file(fd)
            after_read = os.fstat(fd)
            stable_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_mode", "st_uid", "st_gid", "st_nlink")
            if any(getattr(opened, key) != getattr(after_read, key) for key in stable_fields):
                raise InstallError(f"active settings changed while validating staging entry {alias}; left untouched")
        finally:
            os.close(fd)
        if data != expected_new_hook_payload(provider):
            raise InstallError(
                f"possible provider staging entry {alias} does not match the installer's expected {provider} settings; left untouched"
            )

        # Recheck the candidate, but never unlink it: a pathname can be replaced
        # after any lstat/identity check, and unlink would then delete user data.
        current_active = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        current_alias = os.stat(alias, dir_fd=parent_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(current_active.st_mode)
            or not stat.S_ISREG(current_alias.st_mode)
            or (current_active.st_dev, current_active.st_ino) != (active_info.st_dev, active_info.st_ino)
            or (current_alias.st_dev, current_alias.st_ino) != (active_info.st_dev, active_info.st_ino)
            or current_active.st_nlink != 2
            or current_alias.st_nlink != 2
            or (current_active.st_uid, current_active.st_gid) != expected_owner
            or (current_alias.st_uid, current_alias.st_gid) != expected_owner
            or stat.S_IMODE(current_active.st_mode) != expected_mode
            or stat.S_IMODE(current_alias.st_mode) != expected_mode
        ):
            raise InstallError(f"provider staging entry {alias} changed during validation; left untouched")
        if not verify_parent(root, root_fd, relative, parent_fd):
            raise InstallError(f"target directory moved during staging inspection for {relative}; left untouched")
        if report:
            print(
                f"PRESERVED possible installer staging link {alias} for {relative}; "
                "its origin cannot be proven from its name or metadata, so it was not removed"
            )
        return True
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(root_fd)


def op_merge_hook(root, provider, relative, state_path):
    state = load_state(state_path)
    root_fd = open_root(root)
    parent_fd = None
    temporary = None
    temp_fd = None
    try:
        parent_fd, name = open_parent(root_fd, relative, True, state, state_path)
        if not verify_parent(root, root_fd, relative, parent_fd):
            raise InstallError(f"target directory moved or changed after it was opened; refusing settings write: {relative}")
        original = inspect_existing(parent_fd, name, allow_hardlinks=True)
        config = {}
        if original is not None:
            try:
                config = json.loads(original["data"].decode("utf-8"))
            except Exception as exc:
                raise InstallError(f"cannot safely parse {relative}: {exc}; no settings were changed") from exc
        sessions = validate_config(config, relative)
        entry = hook_entry(provider)
        if hook_is_registered(provider, sessions):
            if original is not None and original["nlink"] > 1:
                if original["nlink"] != 2 or not recover_provider_staging(root, provider, report=False):
                    raise InstallError(
                        f"cannot safely accept multiply-linked {relative}; "
                        "no unique verified provider staging candidate was found"
                    )
            print("HOOK PRESENT")
            return
        if original is not None:
            fail(
                f"cannot safely replace pre-existing {relative}: POSIX has no portable conditional replace for non-cooperative writers; file preserved, merge the hook manually",
                status=11,
            )
        sessions.append(entry)
        payload = (json.dumps(config, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        template = {
            "mode": 0o644,
            "uid": os.geteuid(),
            "gid": os.getegid(),
            "atime_ns": time.time_ns(),
            "mtime_ns": time.time_ns(),
            "xattrs": {},
        }
        if not verify_parent(root, root_fd, relative, parent_fd):
            raise InstallError(f"target directory moved or changed before staging settings: {relative}")
        temporary, temp_fd, expected = new_temp(parent_fd, name, payload, template)
        operation = {"path": relative, "expected": expected}
        state["operations"].append(operation)
        save_state(state_path, state)
        if not verify_parent(root, root_fd, relative, parent_fd):
            raise InstallError(f"target directory moved or changed before publishing settings; staged file was not installed: {relative}")
        if identity(temp_fd) != expected:
            raise InstallError(f"new settings staging inode changed before publishing: {relative}")
        try:
            os.link(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
        except FileExistsError:
            state["operations"].pop()
            save_state(state_path, state)
            raise InstallError(f"{relative} appeared while its hook was being prepared; it was preserved")
        if not verify_parent(root, root_fd, relative, parent_fd):
            print(f"PRESERVED settings written through a directory moved during publication: {relative}", file=sys.stderr)
            raise InstallError(f"target directory moved during settings publication; rollback will preserve uncertain data: {relative}")
        linked = signature_at(parent_fd, name, reject_hardlinks=False)
        if (linked != identity(temp_fd)
                or not namespace_identity_matches(linked, {**expected, "nlink": expected["nlink"] + 1})):
            raise InstallError(f"new settings changed at link publication; preserved: {relative}")
        os.unlink(temporary, dir_fd=parent_fd)
        temporary = None
        record_published_identity(root, root_fd, relative, parent_fd, name,
                                  temp_fd, expected, operation, state, state_path)
        print("HOOK ADDED")
    finally:
        if temp_fd is not None:
            os.close(temp_fd)
        if temporary is not None and parent_fd is not None:
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except OSError:
                pass
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(root_fd)


def signature_at(parent_fd, name, *, reject_hardlinks=True):
    fd = open_existing(parent_fd, name, reject_hardlinks=reject_hardlinks)
    if fd is None:
        return None
    try:
        return identity(fd)
    finally:
        os.close(fd)


def rollback(root, state_path):
    state = load_state(state_path)
    incomplete = False
    root_fd = open_root(root)
    try:
        for operation in reversed(state["operations"]):
            relative = operation["path"]
            parent_fd = None
            quarantine = None
            try:
                parent_fd, name = open_parent(root_fd, relative, False, state, state_path)
                if not verify_parent(root, root_fd, relative, parent_fd):
                    print(f"PRESERVED file; destination directory moved or changed before rollback: {relative}", file=sys.stderr)
                    incomplete = True
                    continue
                if operation.get("confirmation_backup"):
                    # A confirmed exchange has an old inode at the recovery
                    # name.  There is no safe CAS-based rollback: moving the
                    # active entry could overwrite a concurrent writer, while
                    # deleting the backup could destroy the original.
                    print(
                        f"PRESERVED confirmed update and recovery backup; rollback requires manual review: {relative}",
                        file=sys.stderr,
                    )
                    incomplete = True
                    continue
                current = signature_at(parent_fd, name)
                if current is None:
                    continue
                if not same_identity(current, operation["expected"]):
                    print(f"PRESERVED concurrent change; rollback skipped: {relative}", file=sys.stderr)
                    incomplete = True
                    continue
                # Keep the moved inode under a recovery name: another process may
                # still hold a writable descriptor and change it after this check.
                quarantine = f".{name}.rollback-{os.getpid()}-{secrets.token_hex(5)}"
                rename_noreplace(parent_fd, name, quarantine)
                if not verify_parent(root, root_fd, relative, parent_fd):
                    print(f"PRESERVED recovery file in a directory moved during rollback: {relative} ({quarantine})", file=sys.stderr)
                    incomplete = True
                    continue
                moved = signature_at(parent_fd, quarantine)
                # The active name passed the strict ctime check above. Only
                # this immediately observed rename may account for new ctime;
                # never apply this comparison to a stale rollback precheck.
                if not namespace_identity_matches(moved, current):
                    try:
                        os.link(quarantine, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
                    except OSError:
                        print(f"PRESERVED changed file in recovery path {quarantine}: {relative}", file=sys.stderr)
                    incomplete = True
                    continue
                print(f"PRESERVED rollback recovery file {quarantine}: {relative}", file=sys.stderr)
                incomplete = True
            except FileNotFoundError:
                print(f"PRESERVED missing parent; rollback skipped: {relative}", file=sys.stderr)
                incomplete = True
            except OSError as exc:
                print(f"ERROR: rollback could not safely process {relative}: {exc}", file=sys.stderr)
                incomplete = True
            except InstallError as exc:
                print(f"ERROR: rollback could not safely process {relative}: {exc}", file=sys.stderr)
                incomplete = True
            finally:
                if parent_fd is not None:
                    os.close(parent_fd)
        for relative in reversed(state["created_directories"]):
            # A held parent dirfd pins an inode but cannot prove that the inode
            # remains beneath root at the rmdir syscall boundary. Keep even an
            # empty directory rather than risk deleting one moved outside root.
            print(
                f"PRESERVED created directory during rollback; it was not removed because a directory descriptor cannot prove it remains inside the target: {relative}",
                file=sys.stderr,
            )
            incomplete = True
    finally:
        os.close(root_fd)
    return 1 if incomplete else 0


def snapshot_command(root, relative, output):
    root_fd = open_root(root)
    parent_fd = None
    try:
        state = {"operations": [], "created_directories": []}
        parent_fd, name = open_parent(root_fd, relative, False, state, output + ".state-unused")
        current = inspect_existing(parent_fd, name, allow_hardlinks=True)
        if current is None:
            raise InstallError(f"file is missing: {relative}")
        with open(output, "wb") as handle:
            handle.write(current["data"])
    finally:
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(root_fd)
        try:
            os.unlink(output + ".state-unused")
        except FileNotFoundError:
            pass


def confirmation_snapshot(fd, parent_fd):
    """Return a complete, re-checkable snapshot of one uniquely-owned file."""
    before = os.fstat(fd)
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise InstallError("confirmation target must be a regular file with exactly one link")
    data = read_open_file(fd)
    after = os.fstat(fd)
    fields = ("st_dev", "st_ino", "st_size", "st_mode", "st_uid", "st_gid",
              "st_nlink", "st_mtime_ns", "st_ctime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in fields):
        raise InstallError("file changed while the confirmation snapshot was read")
    attrs = xattrs(fd)
    protected = protected_metadata(fd)
    verified_protected = protected_metadata(fd)
    final = os.fstat(fd)
    if any(getattr(after, field) != getattr(final, field) for field in fields) or protected != verified_protected:
        raise InstallError("file changed while the confirmation attributes were read")
    return {
        "inode": {"device": after.st_dev, "number": after.st_ino, "nlink": after.st_nlink},
        "content": {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)},
        "metadata": {
            "mode": stat.S_IMODE(after.st_mode), "uid": after.st_uid, "gid": after.st_gid,
            "mtime_ns": after.st_mtime_ns, "ctime_ns": after.st_ctime_ns,
            "xattrs": attrs if attrs is not None else {},
            "protected": protected,
            "xattrs_supported": attrs is not None,
        },
        "parent": {"device": os.fstat(parent_fd).st_dev, "number": os.fstat(parent_fd).st_ino},
    }, data


CONFIRMATION_PATHS = {
    "AGENTS.md", ".claude/settings.json",
    ".agent-maintenance/flywheel-install.sh",
    ".agent-maintenance/installer-transaction.py",
    ".agent-maintenance/framework-ownership.py",
    ".agent-maintenance/session-start.sh",
    ".agent-maintenance/framework-version.json",
    "docs/project-profile.md",
    "scripts/freeze-review-range.py",
    "scripts/validate_lesson_promotions.py",
    "scripts/watermark.py",
    "scripts/write-frozen-review.py",
    ".github/workflows/lesson-promotion-gate.yml",
}


def validate_confirmation_path(relative):
    if relative not in CONFIRMATION_PATHS:
        raise InstallError(f"confirmation transactions are not permitted for {relative!r}")


def confirmation_identity_matches(actual, expected, allow_exchange_ctime=False):
    """Strictly compare identity; only the existing exchange boundary may waive ctime."""
    if not actual or not expected:
        return False
    for section, fields in (
        ("inode", ("device", "number", "nlink")),
        ("content", ("sha256", "size")),
        ("parent", ("device", "number")),
        ("metadata", ("mode", "uid", "gid", "mtime_ns", "ctime_ns", "xattrs", "xattrs_supported", "protected")),
    ):
        left, right = actual.get(section, {}), expected.get(section, {})
        for field in fields:
            if field == "ctime_ns" and allow_exchange_ctime:
                continue
            if left.get(field) != right.get(field):
                return False
    return True


def _write_bytes_atomic(path, data):
    temporary = f"{path}.tmp-{os.getpid()}-{secrets.token_hex(6)}"
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | NOFOLLOW, 0o600)
        try:
            view = memoryview(data)
            while view:
                count = os.write(fd, view)
                view = view[count:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def proposal_command(root, relative, prefix):
    validate_confirmation_path(relative)
    root_fd = open_root(root)
    parent_fd = None
    fd = None
    try:
        parent_fd, name = open_parent(root_fd, relative, False, {"created_directories": []}, prefix + ".unused")
        if not verify_parent(root, root_fd, relative, parent_fd):
            raise InstallError("target parent changed while preparing confirmation snapshot")
        fd = open_existing(parent_fd, name, reject_hardlinks=True)
        if fd is None:
            raise InstallError(f"file is missing: {relative}")
        snapshot, data = confirmation_snapshot(fd, parent_fd)
        _write_bytes_atomic(prefix + ".original", data)
        _write_bytes_atomic(prefix + ".identity.json", (json.dumps(snapshot, indent=2, sort_keys=True) + "\n").encode())
        print("PROPOSAL SAVED")
    finally:
        if fd is not None:
            os.close(fd)
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(root_fd)


def rename_exchange(parent_fd, left, right):
    if sys.platform == "darwin":
        function_name = "renameatx_np"  # RENAME_SWAP
    elif sys.platform.startswith("linux"):
        function_name = "renameat2"  # RENAME_EXCHANGE
    else:
        raise InstallError("atomic exchange is unavailable on this platform; refusing confirmed write")
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        function = getattr(libc, function_name)
    except AttributeError as exc:
        raise InstallError("atomic exchange is unavailable; refusing confirmed write") from exc
    function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    function.restype = ctypes.c_int
    if function(parent_fd, os.fsencode(left), parent_fd, os.fsencode(right), 0x2) != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), right)


def proposal_material_snapshot(path):
    parent = os.open(os.path.dirname(os.path.abspath(path)), DIR_FLAGS | NOFOLLOW)
    fd = None
    try:
        fd = open_existing(parent, os.path.basename(path), reject_hardlinks=True)
        if fd is None:
            raise InstallError("proposal material disappeared")
        return confirmation_snapshot(fd, parent)
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def proposal_material_command(prefix, staged, verify=False):
    with open(prefix + ".identity.json", encoding="utf-8") as handle:
        original_expected = json.load(handle)
    baseline, baseline_data = proposal_material_snapshot(prefix + ".original")
    proposed, _ = proposal_material_snapshot(staged)
    if hashlib.sha256(baseline_data).hexdigest() != original_expected["content"]["sha256"]:
        raise InstallError("display baseline no longer matches the original proposal")
    material = {"baseline": baseline, "proposed": proposed}
    manifest = prefix + ".display.json"
    if verify:
        with open(manifest, encoding="utf-8") as handle:
            if json.load(handle) != material:
                raise InstallError("displayed proposal material changed; refusing confirmation")
    else:
        _write_bytes_atomic(manifest, (json.dumps(material, sort_keys=True) + "\n").encode())
    return material


def apply_proposal_command(root, relative, prefix, staged, state_path, require_display_manifest=False):
    validate_confirmation_path(relative)
    if require_display_manifest:
        displayed = proposal_material_command(prefix, staged, verify=True)
    else:
        # Direct Python callers are trusted authorizers of their supplied bytes
        # (as documented at this module's entry); the shell CLI must prove the
        # version shown on the terminal, rather than authorizing bytes afresh.
        displayed = None
    try:
        with open(prefix + ".identity.json", encoding="utf-8") as handle:
            expected = json.load(handle)
        with open(prefix + ".original", "rb") as handle:
            original_data = handle.read()
        if hashlib.sha256(original_data).hexdigest() != expected["content"]["sha256"]:
            raise InstallError("confirmation baseline does not match its snapshot")
        if not expected["metadata"]["xattrs_supported"]:
            raise InstallError("cannot preserve unknown extended attributes; refusing confirmed update")
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise InstallError(f"invalid confirmation snapshot: {exc}") from exc
    root_fd = open_root(root)
    parent_fd = None
    target_fd = None
    stage_fd = None
    temp_fd = None
    backup = None
    exchanged = False
    try:
        parent_fd, name = open_parent(root_fd, relative, False, {"created_directories": []}, prefix + ".unused")
        if not verify_parent(root, root_fd, relative, parent_fd):
            raise InstallError("target parent was replaced after confirmation")
        target_fd = open_existing(parent_fd, name, reject_hardlinks=True)
        if target_fd is None:
            raise InstallError("confirmed target disappeared")
        current, _ = confirmation_snapshot(target_fd, parent_fd)
        if current != expected or current["parent"] != expected.get("parent"):
            raise InstallError("confirmed file or parent changed; refusing update")
        stage_fd = os.open(staged, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | NOFOLLOW | getattr(os, "O_NONBLOCK", 0))
        stage_info = os.fstat(stage_fd)
        if not stat.S_ISREG(stage_info.st_mode) or stage_info.st_nlink != 1:
            raise InstallError("staged file must be a regular file with exactly one link")
        stage_data = read_open_file(stage_fd)
        if displayed is not None:
            stage_parent = os.open(os.path.dirname(os.path.abspath(staged)), DIR_FLAGS | NOFOLLOW)
            try:
                stage_snapshot, _ = confirmation_snapshot(stage_fd, stage_parent)
            finally:
                os.close(stage_parent)
            proposed_content = displayed["proposed"]["content"]
            if (stage_snapshot != displayed["proposed"]
                    or len(stage_data) != proposed_content["size"]
                    or hashlib.sha256(stage_data).hexdigest() != proposed_content["sha256"]):
                raise InstallError("staged file is not the displayed version")
        stage_after = os.fstat(stage_fd)
        if stage_after.st_size != len(stage_data) or stage_after.st_ino != stage_info.st_ino:
            raise InstallError("staged file changed while being read")
        if not verify_parent(root, root_fd, relative, parent_fd):
            raise InstallError("target parent changed before confirmed update")
        # Re-open by pathname at the last safe boundary; a held fd pins the old
        # inode and cannot detect pathname replacement.
        latest_fd = open_existing(parent_fd, name, reject_hardlinks=True)
        if latest_fd is None:
            raise InstallError("confirmed target disappeared before update")
        try:
            latest, _ = confirmation_snapshot(latest_fd, parent_fd)
        finally:
            os.close(latest_fd)
        if latest != expected:
            raise InstallError("confirmed file changed before update")

        os.close(target_fd)
        target_fd = None

        # The new file is created directly at its final recovery name.  After
        # exchange that name is the displaced original; neither exchanged name
        # is ever unlinked by cleanup.
        backup = f".{name}.confirm-backup-{os.getpid()}-{secrets.token_hex(5)}"
        temporary, temp_fd, new_expected = new_temp(
            parent_fd, name, stage_data, expected["metadata"],
            temporary_name=backup, preserve_on_failure=True,
        )
        if temporary != backup:
            raise InstallError("staging name mismatch")
        new_expected, _ = confirmation_snapshot(temp_fd, parent_fd)
        state = load_state(state_path)
        state["operations"].append({
            "path": relative,
            "status": "pending",
            "old_expected": expected,
            "expected": new_expected,
            "confirmation_backup": backup,
        })
        save_state(state_path, state)
        # Staging and journal I/O must not open an unchecked target/version
        # window. Re-open both names after that work and keep the staging fd
        # pinned through exchange. This narrows, but is not a namespace CAS.
        latest_fd = open_existing(parent_fd, name, reject_hardlinks=True)
        staged_name_fd = open_existing(parent_fd, backup, reject_hardlinks=True)
        try:
            if latest_fd is None or staged_name_fd is None:
                raise InstallError("confirmed or staged inode disappeared before exchange")
            final_target, _ = confirmation_snapshot(latest_fd, parent_fd)
            final_stage, _ = confirmation_snapshot(staged_name_fd, parent_fd)
            if final_target != expected or final_stage != new_expected:
                raise InstallError("confirmed or staged version changed before exchange")
            if not verify_parent(root, root_fd, relative, parent_fd):
                raise InstallError("target parent changed before exchange")
        finally:
            if latest_fd is not None:
                os.close(latest_fd)
            if staged_name_fd is not None:
                os.close(staged_name_fd)
        rename_exchange(parent_fd, name, backup)
        exchanged = True
        os.close(temp_fd)
        temp_fd = None

        # ctime changes on rename, so compare all stable identity/data fields
        # while explicitly retaining inode identity and the displaced bytes.
        displaced_fd = None
        active_fd = None
        try:
            displaced_fd = open_existing(parent_fd, backup, reject_hardlinks=True)
            active_fd = open_existing(parent_fd, name, reject_hardlinks=True)
            if displaced_fd is None or active_fd is None:
                raise InstallError("exchange did not leave both active and recovery entries")
            displaced, _ = confirmation_snapshot(displaced_fd, parent_fd)
            active, _ = confirmation_snapshot(active_fd, parent_fd)
        finally:
            if displaced_fd is not None:
                os.close(displaced_fd)
            if active_fd is not None:
                os.close(active_fd)
        if not confirmation_identity_matches(displaced, expected, allow_exchange_ctime=True):
            raise InstallError("exchange displaced inode or data did not match the confirmed original")
        if not confirmation_identity_matches(active, new_expected, allow_exchange_ctime=True):
            raise InstallError("active pathname does not contain the staged inode or data")
        if not verify_parent(root, root_fd, relative, parent_fd):
            raise InstallError("target parent moved after confirmed update; recovery entries preserved")

        state = load_state(state_path)
        operation = state["operations"][-1]
        operation["status"] = "applied"
        operation["expected"] = active
        operation["new_expected"] = active
        save_state(state_path, state)
        print(f"APPLIED (recovery backup: {backup})")
    finally:
        if stage_fd is not None:
            os.close(stage_fd)
        if target_fd is not None:
            os.close(target_fd)
        if temp_fd is not None:
            os.close(temp_fd)
        # Even before exchange, another process may replace the staging name.
        # Never unlink it based on an earlier pathname/identity observation.
        if backup is not None:
            phase = "backup" if exchanged else "staging/recovery entry"
            print(f"PRESERVED confirmation {phase}: {backup}", file=sys.stderr)
        if parent_fd is not None:
            os.close(parent_fd)
        os.close(root_fd)


def main(argv):
    if not argv:
        fail("missing operation")
    command = argv[0]
    try:
        if command == "lock-exec" and len(argv) >= 4:
            lock_and_exec(argv[1], argv[2:])
        elif command == "verify-lock" and len(argv) == 3:
            verify_target_lock(argv[1], argv[2])
        elif command == "recover-hook-staging" and len(argv) == 3:
            recover_provider_staging(argv[1], argv[2])
        elif command == "validate-hook-staging" and len(argv) == 3:
            if not recover_provider_staging(argv[1], argv[2], report=False):
                raise InstallError(f"no verified provider staging candidate exists for {HOOK_SETTINGS.get(argv[2], argv[2])}")
        elif command == "init" and len(argv) == 3:
            root_fd = open_root(argv[1])
            os.close(root_fd)
            save_state(argv[2], {"operations": [], "created_directories": []})
        elif command == "snapshot" and len(argv) == 4:
            snapshot_command(argv[1], argv[2], argv[3])
        elif command == "proposal" and len(argv) == 4:
            proposal_command(argv[1], argv[2], argv[3])
        elif command == "seal-display" and len(argv) == 3:
            proposal_material_command(argv[1], argv[2])
        elif command == "verify-display" and len(argv) == 3:
            proposal_material_command(argv[1], argv[2], verify=True)
        elif command == "apply-proposal" and len(argv) == 6:
            apply_proposal_command(argv[1], argv[2], argv[3], argv[4], argv[5], require_display_manifest=True)
        elif command == "compare" and len(argv) == 4:
            return compare_command(argv[1], argv[2], argv[3])
        elif command == "copy" and len(argv) == 5:
            op_copy(argv[1], argv[2], argv[3], argv[4])
        elif command == "merge-hook" and len(argv) == 5:
            op_merge_hook(argv[1], argv[2], argv[3], argv[4])
        elif command == "rollback" and len(argv) == 3:
            return rollback(argv[1], argv[2])
        else:
            fail(f"invalid operation or arguments: {command}")
    except SystemExit:
        raise
    except (OSError, ValueError, json.JSONDecodeError, InstallError) as exc:
        fail(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

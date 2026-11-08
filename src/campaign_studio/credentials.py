"""One local OpenAI credential source; no network validation or campaign storage."""

import io
import os
import stat
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from uuid import uuid4

from dotenv import dotenv_values
from dotenv.parser import parse_stream

from campaign_studio.local_files import file_lock, has_link, publish_file

KEY = "OPENAI_API_KEY"
KEY_LIMIT = 4096
BODY_LIMIT = 8192
ENV_LIMIT = 131072


class CredentialError(Exception):
    def __init__(self, code, message, status=503):
        super().__init__(message)
        self.code, self.status = code, status


def invalid_key():
    return CredentialError(
        "credential_input",
        "Enter one API key without spaces or line breaks, then try again.",
        422,
    )


def safe_file(info):
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_uid == os.getuid()
        and info.st_nlink == 1
        and not info.st_mode & 0o022
    )


@contextmanager
def directory(path):
    """Open each path component without following a directory symlink."""
    descriptor = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != os.getuid() or info.st_mode & 0o022:
            raise OSError("Unsafe credential directory")
        yield descriptor
    finally:
        os.close(descriptor)


class UnreadableProtectedKey(ValueError):
    """DPAPI could not decrypt a stored key in the current Windows account."""


def read_saved_key(path):
    if has_link(path):
        raise OSError("Unsafe credential path")
    if os.name != "nt":
        with directory(path.parent) as parent:
            _, payload = LocalCredentials(path)._read(parent)
        return dotenv_values(stream=io.StringIO(payload.decode()), interpolate=False).get(KEY)
    from campaign_studio.windows_key import protect

    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > ENV_LIMIT:
        raise OSError("Unsafe credential file")
    encrypted = path.read_bytes()
    try:
        return protect(encrypted, decrypt=True).decode("ascii")
    except (OSError, UnicodeError) as error:
        raise UnreadableProtectedKey("Windows could not decrypt this saved key.") from error


class LocalCredentials:
    def __init__(self, destination=None, *, managed_by_environment=False, environment=None):
        # None deliberately disables persistence for injected/test application
        # instances. Only normal launch configuration or a test supplies a path.
        self.destination = Path(destination).absolute() if destination is not None else None
        self.managed_by_environment = managed_by_environment
        self.environment = os.environ if environment is None else environment
        self.loaded_at = time.time() if managed_by_environment and self.current_key() else None
        self.saved_at = None
        if (
            self.destination is not None
            and not managed_by_environment
            and not has_link(self.destination)
        ):
            with suppress(OSError):
                self.saved_at = self.destination.stat().st_mtime

    def current_key(self):
        return self.environment.get(KEY, "")

    def status(self):
        return {
            "available": bool(self.current_key()),
            "saved_at": self.saved_at,
            "managed_by_environment": self.managed_by_environment,
            "can_save": self.destination is not None and not self.managed_by_environment,
        }

    def _read(self, parent):
        try:
            descriptor = os.open(
                self.destination.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent
            )
        except FileNotFoundError:
            return None, b""
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not safe_file(before) or before.st_size > ENV_LIMIT:
                raise OSError("Unsafe credential file")
            value = stream.read(ENV_LIMIT + 1)
            after = os.fstat(stream.fileno())

            def identity(info):
                return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

            if len(value) > ENV_LIMIT or identity(before) != identity(after):
                raise OSError("Credential file changed")
            return identity(after), value

    @staticmethod
    def _updated(previous, value):
        text = previous.decode("utf-8")
        bindings = list(parse_stream(io.StringIO(text)))
        if "\0" in text or any(item.error for item in bindings):
            raise CredentialError(
                "credential_file_invalid",
                "The local .env file needs attention. Correct its syntax before saving a key.",
            )
        encoded = value.replace("\\", "\\\\").replace("'", "\\'") if value is not None else ""
        assignment = f"{KEY}='{encoded}'\n" if value is not None else ""
        result, replaced = [], False
        for item in bindings:
            if item.key != KEY:
                result.append(item.original.string)
            elif not replaced:
                result.append(assignment)
                replaced = True
            # Remove duplicate assignments so reopening cannot restore an old key.
        if not replaced and value is not None:
            if text and not text.endswith("\n"):
                result.append("\n")
            result.append(assignment)
        payload = "".join(result).encode("utf-8")
        if len(payload) > ENV_LIMIT:
            raise OSError("Credential file too large")
        return payload

    def _replace(self, parent, payload, expected):
        temporary = f".campaign-studio-key-{uuid4().hex}.tmp"
        try:
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            with os.fdopen(descriptor, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            # External edits are not participants in our lock. Recheck after
            # preparing the temporary file, immediately before replacement.
            with directory(self.destination.parent) as current:
                before, now = os.fstat(parent), os.fstat(current)
                if (before.st_dev, before.st_ino) != (now.st_dev, now.st_ino):
                    raise OSError("Credential directory changed")
            if self._read(parent) != expected:
                raise OSError("Credential file changed")
            os.replace(temporary, self.destination.name, src_dir_fd=parent, dst_dir_fd=parent)
        finally:
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=parent)

    def _persist_windows(self, value):
        from campaign_studio.windows_key import protect

        target = self.destination
        lock = target.with_name("key-save.lock")
        if has_link(target) or has_link(lock):
            raise OSError("Unsafe credential path")
        with lock.open("a+b") as stream:
            if os.fstat(stream.fileno()).st_nlink != 1:
                raise OSError("Unsafe credential lock")
            with file_lock(stream, wait=False):
                before = target.stat() if target.exists() else None
                if before and (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1):
                    raise OSError("Unsafe credential file")
                if value is None:
                    target.unlink(missing_ok=True)
                    self.environment.pop(KEY, None)
                    return
                temporary = target.with_name("key-" + uuid4().hex + ".tmp")
                with temporary.open("xb") as output:
                    try:
                        output.write(protect(value.encode("ascii")))
                        output.flush()
                        os.fsync(output.fileno())
                    except BaseException:
                        output.close()
                        temporary.unlink(missing_ok=True)
                        raise
                try:
                    if has_link(target) or (target.stat() if target.exists() else None) != before:
                        raise OSError("Credential file changed")
                    publish_file(temporary, target, replace=before is not None)
                    self.environment[KEY] = value
                finally:
                    temporary.unlink(missing_ok=True)

    def _persist(self, value):
        if os.name == "nt":
            try:
                self._persist_windows(value)
            except BlockingIOError as error:
                raise CredentialError(
                    "credential_busy", "Another key save is in progress. Try again shortly.", 409
                ) from error
            return
        import fcntl

        with directory(self.destination.parent) as parent:
            lock = os.open(
                ".campaign-studio-key.lock",
                os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent,
            )
            with os.fdopen(lock, "rb") as stream:
                if not safe_file(os.fstat(stream.fileno())):
                    raise OSError("Unsafe credential lock")
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise CredentialError(
                        "credential_busy",
                        "Another key save is in progress. Try again shortly.",
                        409,
                    ) from None
                identity, previous = self._read(parent)
                payload = self._updated(previous, value)
                self._replace(parent, payload, (identity, previous))
                try:
                    os.fsync(parent)
                except OSError:
                    # Keep no secret backup file. Restore the prior bytes from
                    # memory only while the published value is still ours.
                    try:
                        published = self._read(parent)
                        if published[1] != payload:
                            raise OSError("Credential file changed after publication")
                        if identity is None:
                            os.unlink(self.destination.name, dir_fd=parent)
                        else:
                            self._replace(parent, previous, published)
                        os.fsync(parent)
                    except OSError:
                        raise CredentialError(
                            "credential_save_uncertain",
                            "The local key save could not be confirmed. The running key was not "
                            "changed. Check the local .env file before trying again.",
                        ) from None
                    raise OSError("Credential publication was rolled back") from None
                # Activation belongs inside the same lock, after both fsyncs.
                # Already constructed providers retain their original credential.
                if value is None:
                    self.environment.pop(KEY, None)
                else:
                    self.environment[KEY] = value

    def remove(self):
        if self.managed_by_environment:
            raise CredentialError(
                "credential_environment_managed",
                "This key is managed outside Motif. Remove it from the launch environment "
                "and restart the app.",
                409,
            )
        if self.destination is None:
            raise CredentialError(
                "credential_setup_unavailable",
                "Local key management is unavailable in this session.",
            )
        try:
            self._persist(None)
        except OSError, UnicodeError:
            raise CredentialError(
                "credential_remove_failed",
                "The key could not be removed. Check file permissions and try again. "
                "The running key is unchanged.",
            ) from None
        self.saved_at = self.loaded_at = None
        return {"removed": True, **self.status()}

    def save(self, value):
        if (
            type(value) is not str
            or not 1 <= len(value) <= KEY_LIMIT
            or any(not 33 <= ord(character) <= 126 for character in value)
        ):
            raise invalid_key()
        if self.managed_by_environment:
            raise CredentialError(
                "credential_environment_managed",
                "This key is managed by the launch environment. Update that environment and "
                "restart the app to use a different key.",
                409,
            )
        if self.destination is None:
            raise CredentialError(
                "credential_setup_unavailable",
                "Local key setup is unavailable in this session. "
                "Use the documented environment setup.",
            )
        try:
            self._persist(value)
        except OSError, UnicodeError:
            raise CredentialError(
                "credential_save_failed",
                "The key could not be saved locally. Check the key file's folder permissions "
                "and free disk space, then try again. The running key is unchanged.",
            ) from None
        self.saved_at = time.time()
        return {"saved": True, **self.status()}

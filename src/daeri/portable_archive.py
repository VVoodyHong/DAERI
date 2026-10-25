"""Authenticated, bounded transport for one profile's logical archive."""

import hashlib
import json
import os
import re
import stat
import struct
import tempfile
import zipfile
import zlib
from contextlib import contextmanager
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from .storage import StoreError

MAGIC = b'DAERI-PROFILE\x00\x01'
MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_MEMBERS = 100000
CHUNK_BYTES = 128 * 1024
_BLOB = re.compile(r'blobs/[0-9a-f]{64}')
_HEADER_BYTES = len(MAGIC) + 16 + 12


def _require(condition):
    if not condition:
        raise StoreError('INVALID_INPUT', 'The profile archive or passphrase is invalid.')


def _password(passphrase):
    try:
        _require(isinstance(passphrase, str))
        encoded = passphrase.encode('utf-8')
        _require(8 <= len(encoded) <= 1024)
        return encoded
    except UnicodeError:
        raise StoreError('INVALID_INPUT', 'The profile archive or passphrase is invalid.') from None


def _key(password, salt):
    return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password)


def _json_depth(value, depth=0):
    _require(depth <= 64)
    if isinstance(value, dict):
        _require(all(isinstance(key, str) for key in value))
        for child in value.values():
            _json_depth(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            _json_depth(child, depth + 1)
    else:
        _require(value is None or type(value) in (str, bool, int, float))


def _manifest_bytes(manifest):
    _require(isinstance(manifest, dict))
    _json_depth(manifest)
    encoded = json.dumps(manifest, ensure_ascii=False, allow_nan=False, sort_keys=True,
                         separators=(',', ':')).encode('utf-8')
    _require(len(encoded) <= MAX_MANIFEST_BYTES)
    return encoded


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result)
        result[key] = value
    return result


def _constant(value):
    raise ValueError('Invalid JSON number.')


@contextmanager
def _regular(path):
    try:
        path = Path(path)
        _require(not path.is_symlink())
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
                             | getattr(os, 'O_NONBLOCK', 0))
    except OSError:
        raise StoreError('INVALID_INPUT', 'A regular profile archive or blob file is required.') from None
    try:
        _require(stat.S_ISREG(os.fstat(descriptor).st_mode))
    except BaseException:
        os.close(descriptor)
        raise
    with os.fdopen(descriptor, 'rb') as stream:
        yield stream


def _private_file(path):
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.chmod(path, 0o600)
        return os.fdopen(descriptor, 'wb')
    except BaseException:
        os.close(descriptor)
        raise


def _zip_write(path, manifest, blobs):
    _require(isinstance(blobs, dict) and len(blobs) + 1 <= MAX_MEMBERS)
    _require(all(isinstance(name, str) and _BLOB.fullmatch(name) for name in blobs))
    content = _manifest_bytes(manifest)
    total = len(content)
    _require(total <= MAX_UNCOMPRESSED_BYTES)
    with _private_file(path) as outgoing, zipfile.ZipFile(outgoing, 'w', compression=zipfile.ZIP_STORED) as archive:
        archive.writestr('manifest.json', content)
        for name, source in blobs.items():
            digest = hashlib.sha256()
            with _regular(source) as incoming, archive.open(name, 'w', force_zip64=True) as member:
                while chunk := incoming.read(CHUNK_BYTES):
                    total += len(chunk)
                    _require(total <= MAX_UNCOMPRESSED_BYTES)
                    digest.update(chunk)
                    member.write(chunk)
            _require(digest.hexdigest() == name[6:])


def _encrypt(source, destination, password):
    salt, nonce = os.urandom(16), os.urandom(12)
    header = MAGIC + salt + nonce
    encryptor = Cipher(algorithms.AES256(_key(password, salt)), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header)
    _require(source.stat().st_size + len(header) + 16 <= MAX_ARCHIVE_BYTES)
    with source.open('rb') as incoming, _private_file(destination) as outgoing:
        outgoing.write(header)
        while chunk := incoming.read(CHUNK_BYTES):
            outgoing.write(encryptor.update(chunk))
        outgoing.write(encryptor.finalize())
        outgoing.write(encryptor.tag)
        outgoing.flush()
        os.fsync(outgoing.fileno())


def write_bundle(path: Path, manifest: dict, blobs: dict[str, Path], passphrase: str):
    """Publish a complete private archive without replacing any existing path."""
    password = _password(passphrase)
    try:
        path = Path(path)
        _require(not path.exists() and not path.is_symlink())
        with tempfile.TemporaryDirectory(prefix='.daeri-archive-', dir=path.parent) as temporary:
            folder = Path(temporary)
            folder.chmod(0o700)
            plaintext, encrypted = folder / 'payload.zip', folder / 'encrypted'
            _zip_write(plaintext, manifest, blobs)
            _encrypt(plaintext, encrypted, password)
            try:
                os.link(encrypted, path)
            except FileExistsError:
                raise StoreError('INVALID_INPUT', 'The archive destination already exists.') from None
            if os.name != 'nt':
                descriptor = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise StoreError('INVALID_INPUT', 'The profile archive input is invalid.') from None
    except OSError:
        raise StoreError('STORE_UNAVAILABLE', 'The profile archive could not be published.') from None


def _decrypt(source, destination, password):
    with _regular(source) as incoming:
        size = os.fstat(incoming.fileno()).st_size
        _require(_HEADER_BYTES + 16 <= size <= MAX_ARCHIVE_BYTES)
        header = incoming.read(_HEADER_BYTES)
        _require(len(header) == _HEADER_BYTES and header.startswith(MAGIC))
        salt, nonce = header[len(MAGIC):len(MAGIC) + 16], header[-12:]
        incoming.seek(-16, os.SEEK_END)
        tag = incoming.read(16)
        incoming.seek(_HEADER_BYTES)
        remaining = size - _HEADER_BYTES - 16
        decryptor = Cipher(algorithms.AES256(_key(password, salt)), modes.GCM(nonce, tag)).decryptor()
        decryptor.authenticate_additional_data(header)
        with _private_file(destination) as outgoing:
            while remaining:
                chunk = incoming.read(min(CHUNK_BYTES, remaining))
                _require(bool(chunk))
                remaining -= len(chunk)
                outgoing.write(decryptor.update(chunk))
            # Unauthenticated plaintext stays private and is never parsed or yielded.
            outgoing.write(decryptor.finalize())


def _zip_directory_limit(path):
    """Bound the central directory before ZipFile allocates its member objects."""
    with path.open('rb') as stream:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(max(0, size - 65557))
        tail = stream.read()
        position = tail.rfind(b'PK\x05\x06')
        _require(position >= 0 and len(tail) - position >= 22)
        _, disk, central_disk, disk_count, count, central_size, offset, comment_size = struct.unpack(
            '<4s4H2LH', tail[position:position + 22])
        _require(position + 22 + comment_size == len(tail) and disk == central_disk == 0)
        end_offset = size - len(tail) + position
        central_end = end_offset
        locator = b''
        if end_offset >= 20:
            stream.seek(end_offset - 20)
            locator = stream.read(20)
        has_zip64 = locator.startswith(b'PK\x06\x07')
        _require(has_zip64 or (count != 65535 and central_size != 0xffffffff and offset != 0xffffffff))
        if has_zip64:
            signature, disk, zip64_offset, disks = struct.unpack('<4sLQL', locator)
            _require(signature == b'PK\x06\x07' and disk == 0 and disks == 1
                     and zip64_offset + 56 <= end_offset - 20)
            stream.seek(zip64_offset)
            record = stream.read(56)
            _require(len(record) == 56)
            signature, length, _, _, disk, central_disk, disk_count, count, central_size, offset = struct.unpack(
                '<4sQ2H2L4Q', record)
            # Match ZipFile's fixed ZIP64 record so nested records cannot change its bounds.
            _require(signature == b'PK\x06\x06' and length == 44 and disk == central_disk == 0
                     and zip64_offset + 12 + length == end_offset - 20)
            central_end = zip64_offset
        _require(0 < count <= MAX_MEMBERS and disk_count == count
                 and central_size <= count * 256 and offset + central_size == central_end)
        stream.seek(offset)
        for _ in range(count):
            header = stream.read(46)
            _require(len(header) == 46 and header.startswith(b'PK\x01\x02'))
            lengths = struct.unpack('<3H', header[28:34])
            _require(stream.tell() + sum(lengths) <= central_end)
            stream.seek(sum(lengths), os.SEEK_CUR)
        _require(stream.tell() == central_end)


def _validate_zip(archive):
    members = archive.infolist()
    _require(0 < len(members) <= MAX_MEMBERS)
    names, total = set(), 0
    for member in members:
        name = member.filename
        _require(name == member.orig_filename and name not in names
                 and (name == 'manifest.json' or _BLOB.fullmatch(name))
                 and stat.S_IFMT(member.external_attr >> 16) in (0, stat.S_IFREG)
                 and not member.flag_bits & 1
                 and member.compress_type in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED))
        names.add(name)
        total += member.file_size
        _require(total <= MAX_UNCOMPRESSED_BYTES)
        if name == 'manifest.json':
            _require(member.file_size <= MAX_MANIFEST_BYTES)
    _require('manifest.json' in names)
    raw = archive.read('manifest.json')
    manifest = json.loads(raw.decode('utf-8'), object_pairs_hook=_pairs, parse_constant=_constant)
    _manifest_bytes(manifest)
    for member in members:
        if member.filename == 'manifest.json':
            continue
        digest, size = hashlib.sha256(), 0
        with archive.open(member) as incoming:
            while chunk := incoming.read(CHUNK_BYTES):
                size += len(chunk)
                _require(size <= member.file_size)
                digest.update(chunk)
        _require(size == member.file_size and digest.hexdigest() == member.filename[6:])
    return manifest


@contextmanager
def open_bundle(path: Path, passphrase: str):
    """Yield only an authenticated ZIP with bounded, verified members."""
    password = _password(passphrase)
    try:
        temporary = tempfile.TemporaryDirectory(prefix='daeri-archive-')
    except OSError:
        raise StoreError('STORE_UNAVAILABLE', 'Private archive storage is unavailable.') from None
    failed = False
    try:
        archive = None
        try:
            folder = Path(temporary.name)
            folder.chmod(0o700)
            plaintext = folder / 'payload.zip'
            _decrypt(path, plaintext, password)
            _zip_directory_limit(plaintext)
            archive = zipfile.ZipFile(plaintext)
            manifest = _validate_zip(archive)
        except OSError:
            if archive is not None:
                archive.close()
            raise StoreError('STORE_UNAVAILABLE', 'Private archive storage is unavailable.') from None
        except (ValueError, TypeError, UnicodeError, RecursionError, struct.error,
                zipfile.BadZipFile, zlib.error, NotImplementedError, InvalidTag):
            if archive is not None:
                archive.close()
            raise StoreError('INVALID_INPUT', 'The profile archive or passphrase is invalid.') from None
        except BaseException:
            if archive is not None:
                archive.close()
            raise
        try:
            yield manifest, archive
        finally:
            archive.close()
    except BaseException:
        failed = True
        raise
    finally:
        try:
            temporary.cleanup()
        except OSError:
            if not failed:
                raise StoreError('STORE_UNAVAILABLE', 'Private archive cleanup is unavailable.') from None

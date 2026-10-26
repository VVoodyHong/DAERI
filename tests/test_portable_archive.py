import hashlib
import io
import json
import os
import stat
import struct
import warnings
import zipfile

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

import daeri.config.portable_archive as portable_archive
from daeri.core.errors import StoreError


PASSPHRASE = 'a private archive passphrase'
MAGIC = b'DAERI-PROFILE\x00\x01'


def blob_name(content):
    return 'blobs/' + hashlib.sha256(content).hexdigest()


def encrypted_zip(path, entries, *, passphrase=PASSPHRASE, central_count=None,
                  corrupt_deflate=False, hidden_zip64=False, nested_zip64=False):
    """Build hostile inputs independently of the production writer."""
    plain = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', UserWarning)
        with zipfile.ZipFile(plain, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for name, content in entries:
                archive.writestr(name, content)
    plaintext = plain.getvalue()
    if hidden_zip64:
        end = struct.unpack('<4s4H2LH', plaintext[-22:])
        central_size, offset, count = end[5], end[6], end[4]
        zip64 = struct.pack('<4sQ2H2L4Q', b'PK\x06\x06', 44, 45, 45, 0, 0,
                            count, count, central_size, offset)
        locator = struct.pack('<4sLQL', b'PK\x06\x07', 0, len(plaintext) - 22, 1)
        misleading = struct.pack('<4s4H2LH', b'PK\x05\x06', 0, 0, 1, 1, 47, offset, 0)
        plaintext = plaintext[:-22] + zip64 + locator + misleading
    if nested_zip64:
        end = struct.unpack('<4s4H2LH', plaintext[-22:])
        central_size, offset, count = end[5], end[6], end[4]
        last_size = 46 + len(entries[-1][0])
        zip64 = struct.pack('<4sQ2H2L4Q', b'PK\x06\x06', 100, 45, 45, 0, 0,
                            1, 1, last_size, offset + central_size - last_size)
        nested = struct.pack('<4sQ2H2L4Q', b'PK\x06\x06', 44, 45, 45, 0, 0,
                             count, count, central_size, offset)
        locator = struct.pack('<4sLQL', b'PK\x06\x07', 0, len(plaintext) - 22, 1)
        sentinel = struct.pack('<4s4H2LH', b'PK\x05\x06', 0, 0, 65535, 65535, 0xffffffff, 0xffffffff, 0)
        plaintext = plaintext[:-22] + zip64 + nested + locator + sentinel
    if central_count is not None:
        plaintext = bytearray(plaintext)
        plaintext[-14:-10] = central_count.to_bytes(2, 'little') * 2
    if corrupt_deflate:
        plaintext = bytearray(plaintext)
        plaintext[30 + len('manifest.json')] = 7
    salt, nonce = bytes(range(16)), bytes(range(12))
    header = MAGIC + salt + nonce
    key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(passphrase.encode())
    path.write_bytes(header + AESGCM(key).encrypt(nonce, plaintext, header))
    return path


def test_bundle_roundtrip_preserves_binary_and_private_permissions(tmp_path):
    source = tmp_path / 'source.bin'
    content = bytes(range(256)) * 1100
    source.write_bytes(content)
    manifest = {'version': 1, 'name': '한글 profile', 'nested': {'unlimited': None}}
    output = tmp_path / 'profile.daeri'

    portable_archive.write_bundle(output, manifest, {blob_name(content): source}, PASSPHRASE)

    assert output.read_bytes().startswith(MAGIC)
    assert b'profile' not in output.read_bytes()[len(MAGIC):]
    if os.name != 'nt':
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
    with portable_archive.open_bundle(output, PASSPHRASE) as (restored, archive):
        assert restored == manifest
        assert archive.read(blob_name(content)) == content
    assert source.read_bytes() == content
    assert sorted(path.name for path in tmp_path.iterdir()) == ['profile.daeri', 'source.bin']


def test_fresh_salt_and_nonce_produce_different_ciphertext(tmp_path):
    first, second = tmp_path / 'first', tmp_path / 'second'
    portable_archive.write_bundle(first, {}, {}, PASSPHRASE)
    portable_archive.write_bundle(second, {}, {}, PASSPHRASE)
    assert first.read_bytes() != second.read_bytes()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX umask and mode contract')
def test_output_keeps_private_read_write_mode_under_a_restrictive_umask(tmp_path):
    output = tmp_path / 'archive'
    previous = os.umask(0o777)
    try:
        portable_archive.write_bundle(output, {}, {}, PASSPHRASE)
    finally:
        os.umask(previous)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def test_writer_uses_the_documented_header_kdf_and_aad(tmp_path):
    output = tmp_path / 'archive'
    portable_archive.write_bundle(output, {'portable': True}, {}, PASSPHRASE)
    content = output.read_bytes()
    header = content[:len(MAGIC) + 28]
    salt, nonce = header[len(MAGIC):len(MAGIC) + 16], header[-12:]
    key = Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(PASSPHRASE.encode())
    plaintext = AESGCM(key).decrypt(nonce, content[len(header):], header)
    with zipfile.ZipFile(io.BytesIO(plaintext)) as archive:
        assert json.loads(archive.read('manifest.json')) == {'portable': True}


def test_reader_accepts_an_independently_encrypted_valid_zip(tmp_path):
    content = b'valid blob'
    path = encrypted_zip(tmp_path / 'archive', [('manifest.json', b'{"version":1}'),
                                                (blob_name(content), content)])
    with portable_archive.open_bundle(path, PASSPHRASE) as (manifest, archive):
        assert manifest == {'version': 1}
        assert archive.read(blob_name(content)) == b'valid blob'


@pytest.mark.parametrize('damage', ['wrong_key', 'magic', 'salt', 'nonce', 'ciphertext', 'tag', 'truncated'])
def test_authentication_failures_never_yield_plaintext(tmp_path, damage):
    path = encrypted_zip(tmp_path / 'archive', [('manifest.json', b'{"secret":"private"}')])
    content = bytearray(path.read_bytes())
    offsets = {'magic': 0, 'salt': len(MAGIC), 'nonce': len(MAGIC) + 16,
               'ciphertext': len(MAGIC) + 28, 'tag': len(content) - 1}
    if damage == 'truncated':
        content = content[:len(MAGIC) + 10]
    elif damage in offsets:
        content[offsets[damage]] ^= 1
    path.write_bytes(content)
    yielded = False
    with pytest.raises(StoreError, match='INVALID_INPUT') as failure:
        with portable_archive.open_bundle(path, 'wrong passphrase' if damage == 'wrong_key' else PASSPHRASE):
            yielded = True
    assert not yielded
    assert 'private' not in failure.value.message
    assert PASSPHRASE not in failure.value.message


@pytest.mark.parametrize('passphrase', ['', 'short', 'é' * 513, '\ud800', None])
def test_invalid_passphrases_are_rejected_without_creating_output(tmp_path, passphrase):
    output = tmp_path / 'archive'
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        portable_archive.write_bundle(output, {}, {}, passphrase)
    assert not output.exists()
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        with portable_archive.open_bundle(output, passphrase):
            pytest.fail('Invalid passphrases must not yield.')


@pytest.mark.parametrize('symlink', [False, True])
def test_output_never_replaces_an_existing_file_or_symlink(tmp_path, symlink):
    original = tmp_path / 'original'
    original.write_bytes(b'keep existing bytes')
    output = tmp_path / 'archive' if symlink else original
    if symlink:
        output.symlink_to(original)
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        portable_archive.write_bundle(output, {}, {}, PASSPHRASE)
    assert original.read_bytes() == b'keep existing bytes'
    assert output.is_symlink() == symlink


@pytest.mark.parametrize('source_kind', ['symlink', 'directory', 'missing', 'digest_mismatch', 'invalid_name'])
def test_writer_rejects_untrusted_blob_sources(tmp_path, source_kind):
    source = tmp_path / 'source'
    source.write_bytes(b'content')
    name = blob_name(b'content')
    if source_kind == 'symlink':
        linked = tmp_path / 'linked'
        linked.symlink_to(source)
        source = linked
    elif source_kind == 'directory':
        source = tmp_path
    elif source_kind == 'missing':
        source = tmp_path / 'missing'
    elif source_kind == 'digest_mismatch':
        name = blob_name(b'different')
    else:
        name = '../outside'
    output = tmp_path / 'archive'
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        portable_archive.write_bundle(output, {}, {name: source}, PASSPHRASE)
    assert not output.exists()


@pytest.mark.parametrize('entries', [
    [('manifest.json', b'{}'), ('../outside', b'bad')],
    [('manifest.json', b'{}'), ('/absolute', b'bad')],
    [('manifest.json', b'{}'), ('blobs\\bad', b'bad')],
    [('manifest.json', b'{}'), ('other.json', b'{}')],
    [('manifest.json', b'{}'), ('manifest.json', b'{}')],
    [('manifest.json', b'{}'), (blob_name(b'x'), b'x'), (blob_name(b'x'), b'x')],
    [('manifest.json', b'{}'), (blob_name(b'x'), b'changed')],
    [(blob_name(b'x'), b'x')],
])
def test_reader_rejects_invalid_members_before_yield(tmp_path, entries):
    path = encrypted_zip(tmp_path / 'archive', entries)
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        with portable_archive.open_bundle(path, PASSPHRASE):
            pytest.fail('Invalid ZIP members must not yield.')


def test_reader_rejects_symlink_zip_members(tmp_path):
    member = zipfile.ZipInfo(blob_name(b'target'))
    member.create_system = 3
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    path = encrypted_zip(tmp_path / 'archive', [('manifest.json', b'{}'), (member, b'target')])
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        with portable_archive.open_bundle(path, PASSPHRASE):
            pytest.fail('ZIP symlinks must not yield.')


def test_reader_rejects_forged_central_directory_counts(tmp_path):
    path = encrypted_zip(tmp_path / 'archive', [('manifest.json', b'{}')], central_count=65535)
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        with portable_archive.open_bundle(path, PASSPHRASE):
            pytest.fail('Unbounded ZIP metadata must not be parsed.')


@pytest.mark.parametrize('kind', ['count', 'hidden_zip64', 'nested_zip64'])
def test_reader_bounds_members_before_zipfile_allocates_objects(tmp_path, monkeypatch, kind):
    entries = [('manifest.json', b'{}'), *((str(index), b'x') for index in range(7))]
    path = encrypted_zip(tmp_path / 'archive', entries, hidden_zip64=kind == 'hidden_zip64',
                         nested_zip64=kind == 'nested_zip64', central_count=2 if kind == 'count' else None)
    monkeypatch.setattr(portable_archive, 'MAX_MEMBERS', 2 if kind == 'count' else 1)

    def forbidden(*args, **kwargs):
        pytest.fail('ZipFile must not allocate members beyond the configured limit.')

    monkeypatch.setattr(zipfile, 'ZipFile', forbidden)
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        with portable_archive.open_bundle(path, PASSPHRASE):
            pytest.fail('Forged directory bounds must not yield.')


def test_reader_reports_invalid_deflate_as_a_safe_archive_error(tmp_path):
    path = encrypted_zip(tmp_path / 'archive', [('manifest.json', b'{}')], corrupt_deflate=True)
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        with portable_archive.open_bundle(path, PASSPHRASE):
            pytest.fail('Invalid deflate data must not yield.')


@pytest.mark.parametrize('raw', [b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'[]',
                                 b'{"x":' + b'[' * 65 + b'0' + b']' * 65 + b'}'])
def test_reader_rejects_ambiguous_or_deep_manifest_json(tmp_path, raw):
    path = encrypted_zip(tmp_path / 'archive', [('manifest.json', raw)])
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        with portable_archive.open_bundle(path, PASSPHRASE):
            pytest.fail('Invalid manifest JSON must not yield.')


@pytest.mark.parametrize('manifest', [[], {'value': float('nan')}, {'value': '\ud800'}])
def test_writer_rejects_invalid_manifest_json(tmp_path, manifest):
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        portable_archive.write_bundle(tmp_path / 'archive', manifest, {}, PASSPHRASE)


@pytest.mark.parametrize('limit,value', [('MAX_ARCHIVE_BYTES', 100), ('MAX_MANIFEST_BYTES', 1),
                                         ('MAX_UNCOMPRESSED_BYTES', 1), ('MAX_MEMBERS', 1)])
def test_reader_enforces_resource_limits_before_yield(tmp_path, monkeypatch, limit, value):
    path = encrypted_zip(tmp_path / 'archive', [('manifest.json', b'{}'), (blob_name(b'x'), b'x')])
    monkeypatch.setattr(portable_archive, limit, value)
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        with portable_archive.open_bundle(path, PASSPHRASE):
            pytest.fail('Oversized archives must not yield.')


@pytest.mark.parametrize('limit,value', [('MAX_ARCHIVE_BYTES', 100), ('MAX_MANIFEST_BYTES', 1),
                                         ('MAX_UNCOMPRESSED_BYTES', 1), ('MAX_MEMBERS', 1)])
def test_writer_enforces_resource_limits_without_publication(tmp_path, monkeypatch, limit, value):
    source = tmp_path / 'source'
    source.write_bytes(b'x')
    output = tmp_path / 'archive'
    monkeypatch.setattr(portable_archive, limit, value)
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        portable_archive.write_bundle(output, {}, {blob_name(b'x'): source}, PASSPHRASE)
    assert not output.exists()


def test_failed_publication_preserves_sources_and_cleans_owned_temps(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.write_bytes(b'content')

    def unavailable(*args, **kwargs):
        raise OSError('private filesystem detail')

    monkeypatch.setattr(portable_archive.os, 'link', unavailable)
    with pytest.raises(StoreError, match='STORE_UNAVAILABLE') as failure:
        portable_archive.write_bundle(tmp_path / 'archive', {}, {blob_name(b'content'): source}, PASSPHRASE)
    assert failure.value.message != 'private filesystem detail'
    assert list(tmp_path.iterdir()) == [source]


def test_blob_output_write_failure_is_reported_as_storage_failure(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.write_bytes(b'content')
    original_open = zipfile.ZipFile.open

    class FailingOutput:
        def __init__(self, member):
            self.member = member

        def __enter__(self):
            return self

        def write(self, content):
            raise OSError('private storage detail')

        def __exit__(self, *args):
            self.member.close()

    def failing_open(archive, name, mode='r', *args, **kwargs):
        member = original_open(archive, name, mode, *args, **kwargs)
        return FailingOutput(member) if mode == 'w' and isinstance(name, str) and name.startswith('blobs/') else member

    monkeypatch.setattr(zipfile.ZipFile, 'open', failing_open)
    with pytest.raises(StoreError, match='STORE_UNAVAILABLE'):
        portable_archive.write_bundle(tmp_path / 'archive', {}, {blob_name(b'content'): source}, PASSPHRASE)
    assert list(tmp_path.iterdir()) == [source]


def test_publication_race_never_replaces_the_competing_file(tmp_path, monkeypatch):
    output = tmp_path / 'archive'
    original_link = os.link

    def competing_link(source, destination):
        output.write_bytes(b'competing output')
        return original_link(source, destination)

    monkeypatch.setattr(portable_archive.os, 'link', competing_link)
    with pytest.raises(StoreError, match='INVALID_INPUT'):
        portable_archive.write_bundle(output, {}, {}, PASSPHRASE)
    assert output.read_bytes() == b'competing output'
    assert list(tmp_path.iterdir()) == [output]


def test_reader_does_not_translate_the_callers_exception(tmp_path):
    output = tmp_path / 'archive'
    portable_archive.write_bundle(output, {}, {}, PASSPHRASE)
    with pytest.raises(ValueError, match='caller failure'):
        with portable_archive.open_bundle(output, PASSPHRASE):
            raise ValueError('caller failure')


def test_reader_reports_private_storage_failure_without_blame_on_archive(tmp_path, monkeypatch):
    output = encrypted_zip(tmp_path / 'archive', [('manifest.json', b'{}')])

    def unavailable(path):
        raise OSError('private temporary storage detail')

    monkeypatch.setattr(portable_archive, '_private_file', unavailable)
    with pytest.raises(StoreError, match='STORE_UNAVAILABLE') as failure:
        with portable_archive.open_bundle(output, PASSPHRASE):
            pytest.fail('Unavailable private storage must not yield.')
    assert 'private temporary storage detail' not in failure.value.message


@pytest.mark.parametrize('caller_failure', [False, True])
def test_reader_sanitizes_cleanup_errors_and_preserves_caller_exceptions(tmp_path, monkeypatch, caller_failure):
    output = encrypted_zip(tmp_path / 'archive', [('manifest.json', b'{}')])
    original = portable_archive.tempfile.TemporaryDirectory

    class FailedCleanup(original):
        def cleanup(self):
            super().cleanup()
            raise OSError('private cleanup detail')

    monkeypatch.setattr(portable_archive.tempfile, 'TemporaryDirectory', FailedCleanup)
    expected, message = (ValueError, 'caller failure') if caller_failure else (StoreError, 'STORE_UNAVAILABLE')
    with pytest.raises(expected, match=message):
        with portable_archive.open_bundle(output, PASSPHRASE):
            if caller_failure:
                raise ValueError('caller failure')


def test_reader_reports_cleanup_failure_inside_an_existing_exception_handler(tmp_path, monkeypatch):
    output = encrypted_zip(tmp_path / 'archive', [('manifest.json', b'{}')])
    original = portable_archive.tempfile.TemporaryDirectory

    class FailedCleanup(original):
        def cleanup(self):
            super().cleanup()
            raise OSError('private cleanup detail')

    monkeypatch.setattr(portable_archive.tempfile, 'TemporaryDirectory', FailedCleanup)
    try:
        raise ValueError('already handled')
    except ValueError:
        with pytest.raises(StoreError, match='STORE_UNAVAILABLE'):
            with portable_archive.open_bundle(output, PASSPHRASE):
                pass

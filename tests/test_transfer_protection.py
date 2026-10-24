"""覆盖传输的故障注入：旧包在发布前始终不变，发布失败可恢复。"""

from __future__ import annotations

import threading

import pytest

from app.config.settings import PACKAGE_MANIFEST_FILENAME, PACKAGE_MBTILES_FILENAME
from tests.android_fakes import fake_result
from tests.test_map_package_transfer import (
    SERIAL,
    _client,
    _make_package,
    _remote_dir,
    _transfer,
)


def _seed_old(client, directory):
    device = client.device(SERIAL)
    device.mkdir_p(directory)
    old = {
        f"{directory}/{PACKAGE_MANIFEST_FILENAME}": b"old manifest",
        f"{directory}/{PACKAGE_MBTILES_FILENAME}": b"old usable map",
        f"{directory}/user.txt": b"keep old package intact on failure",
    }
    device.files.update(old)
    return old


@pytest.mark.parametrize(
    "failure",
    [
        "space",
        "push",
        "manifest",
        "checksum",
        "size",
        "cancel",
        "disconnect",
        "backup",
        "publish",
    ],
)
def test_failed_overwrite_preserves_old_package(work_dir, monkeypatch, failure):
    package = _make_package(work_dir)
    directory = _remote_dir(package)
    kwargs = {
        "fail_push_mbtiles": failure == "push",
        "fail_push_manifest": failure == "manifest",
        "cancel_push": failure == "cancel",
        "disconnect_on_push": failure == "disconnect",
    }
    if failure == "checksum":
        kwargs["sha256_mode"] = "broken"
    if failure == "size":
        kwargs["size_override"] = 1
    client = _client(**kwargs)
    old = _seed_old(client, directory)
    if failure == "space":
        client.device(SERIAL).available_bytes = 1
    move = client.move

    def fail_move(serial, source, target):
        if (failure == "backup" and source == directory) or (
            failure == "publish" and ".upload-" in source and target == directory
        ):
            return fake_result(False, error="injected move failure")
        return move(serial, source, target)

    monkeypatch.setattr(client, "move", fail_move)
    result, _ = _transfer(work_dir, package, client=client, overwrite=True)
    assert not result.success
    assert not result.overwritten
    device = client.device(SERIAL)
    for path, content in old.items():
        assert device.files.get(path) == content
    assert not any(".upload-" in path or ".backup-" in path for path in device.files)


def test_overwrite_keeps_old_package_until_remote_validation(work_dir, monkeypatch):
    package = _make_package(work_dir)
    directory = _remote_dir(package)
    client = _client()
    old = _seed_old(client, directory)
    sha256 = client.remote_sha256
    inspected = []

    def check_old(serial, path):
        assert all(client.device(SERIAL).files.get(key) == value for key, value in old.items())
        inspected.append(path)
        return sha256(serial, path)

    monkeypatch.setattr(client, "remote_sha256", check_old)
    result, _ = _transfer(work_dir, package, client=client, overwrite=True)
    assert result.success, result.errors
    assert inspected and all(".upload-" in path for path in inspected)
    assert result.overwritten and result.checksum_verified
    assert (
        client.device(SERIAL).files[f"{directory}/{PACKAGE_MBTILES_FILENAME}"]
        == (package / PACKAGE_MBTILES_FILENAME).read_bytes()
    )


def test_failed_rollback_retains_backup_and_reports_its_path(work_dir, monkeypatch):
    package = _make_package(work_dir)
    directory = _remote_dir(package)
    client = _client()
    old = _seed_old(client, directory)
    move = client.move

    def fail_publish_and_restore(serial, source, target):
        if target == directory:
            return fake_result(False, error="device unavailable")
        return move(serial, source, target)

    monkeypatch.setattr(client, "move", fail_publish_and_restore)
    result, _ = _transfer(work_dir, package, client=client, overwrite=True)
    assert not result.success
    backups = [path for path in client.device(SERIAL).files if ".backup-" in path]
    assert len(backups) == len(old)
    backup_dir = backups[0].rsplit("/", 1)[0]
    assert backup_dir in " ".join(result.errors)
    for path, content in old.items():
        assert client.device(SERIAL).files[backup_dir + path[len(directory) :]] == content


def test_cancellation_after_validation_does_not_replace_old_package(work_dir, monkeypatch):
    package = _make_package(work_dir)
    directory = _remote_dir(package)
    client = _client()
    old = _seed_old(client, directory)
    cancel = threading.Event()
    sha256 = client.remote_sha256

    def cancel_after_hash(serial, path):
        value = sha256(serial, path)
        cancel.set()
        return value

    monkeypatch.setattr(client, "remote_sha256", cancel_after_hash)
    result, _ = _transfer(work_dir, package, client=client, overwrite=True, cancel_event=cancel)
    assert result.cancelled and not result.success
    assert all(client.device(SERIAL).files.get(key) == value for key, value in old.items())

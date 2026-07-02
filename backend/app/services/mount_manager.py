import asyncio
import logging
import os
import stat
import tempfile

logger = logging.getLogger(__name__)

MOUNT_TIMEOUT_SECONDS = 15


def is_mounted(mount_point: str) -> bool:
    target = os.path.normpath(mount_point)
    try:
        with open("/proc/mounts", "r") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[1] == target:
                    return True
    except OSError:
        pass
    return False


async def _run(cmd: list[str]) -> tuple[bool, str]:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=MOUNT_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, f"Timed out after {MOUNT_TIMEOUT_SECONDS}s running: {' '.join(cmd[:2])}"

    if proc.returncode != 0:
        return False, stderr.decode(errors="ignore").strip() or stdout.decode(errors="ignore").strip()
    return True, "ok"


async def mount_smb(mount_point: str, remote_spec: str, username: str | None, password: str | None) -> tuple[bool, str]:
    os.makedirs(mount_point, exist_ok=True)
    if is_mounted(mount_point):
        return True, "already mounted"

    options = ["vers=3.0"]
    cred_path = None
    if username:
        # Use a credentials file rather than -o username=...,password=... so
        # the password never appears in `ps` output, which any process in
        # the container (or anyone with docker exec) could otherwise read.
        fd, cred_path = tempfile.mkstemp(prefix="lightnvr-cifs-")
        try:
            os.write(fd, f"username={username}\npassword={password or ''}\n".encode())
        finally:
            os.close(fd)
        os.chmod(cred_path, stat.S_IRUSR | stat.S_IWUSR)
        options.append(f"credentials={cred_path}")
    else:
        options.append("guest")

    cmd = ["mount", "-t", "cifs", remote_spec, mount_point, "-o", ",".join(options)]
    try:
        return await _run(cmd)
    finally:
        if cred_path:
            try:
                os.unlink(cred_path)
            except OSError:
                pass


async def mount_nfs(mount_point: str, remote_spec: str) -> tuple[bool, str]:
    os.makedirs(mount_point, exist_ok=True)
    if is_mounted(mount_point):
        return True, "already mounted"

    # `soft` is deliberate, not a default left in place: a `hard` NFS mount
    # (the kernel default) makes any process touching it block
    # uninterruptibly if the server goes away, which would hang the recorder
    # or mover instead of letting storage_manager detect the failure and
    # fail over to backup.
    cmd = ["mount", "-t", "nfs", remote_spec, mount_point, "-o", "soft,timeo=50,retrans=3"]
    return await _run(cmd)


async def mount_share(
    mount_point: str, share_type: str, remote_spec: str, username: str | None, password: str | None
) -> tuple[bool, str]:
    if share_type == "smb":
        return await mount_smb(mount_point, remote_spec, username, password)
    if share_type == "nfs":
        return await mount_nfs(mount_point, remote_spec)
    return False, f"Unknown share type: {share_type}"


async def unmount(mount_point: str) -> tuple[bool, str]:
    if not is_mounted(mount_point):
        return True, "not mounted"

    success, message = await _run(["umount", mount_point])
    if not success:
        # A dead network mount often can't be unmounted cleanly because the
        # kernel tries to flush to a server that's no longer there. Lazy
        # unmount detaches it from the namespace immediately regardless.
        logger.warning("Normal unmount of %s failed (%s), retrying with lazy unmount", mount_point, message)
        success, message = await _run(["umount", "-l", mount_point])
    return success, message


async def remount(
    mount_point: str, share_type: str, remote_spec: str, username: str | None, password: str | None
) -> tuple[bool, str]:
    if is_mounted(mount_point):
        await unmount(mount_point)
    return await mount_share(mount_point, share_type, remote_spec, username, password)

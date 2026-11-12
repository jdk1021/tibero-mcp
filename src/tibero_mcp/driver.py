"""Locate the Tibero JDBC driver and start the JVM.

The driver is TmaxTibero's own build published on Maven Central
(``com.tmaxtibero:tbjdbc8``). It is downloaded once, checked against a pinned
SHA-1, and cached, so the repository never ships a proprietary jar.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import urllib.request
from pathlib import Path

import jpype
import jpype.config

from .config import Settings

log = logging.getLogger(__name__)

DRIVER_GROUP = "com/tmaxtibero"
DRIVER_ARTIFACT = "tbjdbc8"
DRIVER_VERSION = "7.2.6"
DRIVER_SHA1 = "c0fa9b9b43afc8a4c18bdb89acd4d40d8a30bc81"
DRIVER_CLASS = "com.tmax.tibero.jdbc.TbDriver"
MAVEN_CENTRAL = "https://repo1.maven.org/maven2"


class DriverError(RuntimeError):
    pass


def _cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache"
    return Path(base) / "tibero-mcp"


def _sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_driver() -> Path:
    name = f"{DRIVER_ARTIFACT}-{DRIVER_VERSION}.jar"
    target = _cache_dir() / name
    if target.is_file() and _sha1(target) == DRIVER_SHA1:
        return target

    url = f"{MAVEN_CENTRAL}/{DRIVER_GROUP}/{DRIVER_ARTIFACT}/{DRIVER_VERSION}/{name}"
    log.info("Downloading Tibero JDBC driver from %s", url)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=target.parent, suffix=".part")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(url, timeout=60) as resp:
            while chunk := resp.read(1 << 16):
                out.write(chunk)
        digest = _sha1(tmp)
        if digest != DRIVER_SHA1:
            raise DriverError(f"Checksum mismatch for {name}: expected {DRIVER_SHA1}, got {digest}")
        tmp.replace(target)
    except OSError as e:
        raise DriverError(
            f"Could not download the Tibero JDBC driver from Maven Central ({e}). "
            "Check network access, or set TIBERO_JDBC_JAR to a local driver jar."
        ) from e
    finally:
        tmp.unlink(missing_ok=True)
    return target


def resolve_driver(settings: Settings) -> Path:
    if settings.jdbc_jar:
        jar = settings.jdbc_jar.expanduser()
        if not jar.is_file():
            raise DriverError(f"TIBERO_JDBC_JAR points to a missing file: {jar}")
        return jar
    return _download_driver()


def start_jvm(settings: Settings) -> None:
    if jpype.isJVMStarted():
        return
    if settings.java_home:
        os.environ["JAVA_HOME"] = str(settings.java_home.expanduser())
    jar = resolve_driver(settings)
    try:
        jvm_path = jpype.getDefaultJVMPath()
    except jpype.JVMNotFoundException as e:
        raise DriverError("No Java runtime found. Install a JDK/JRE 8 or newer, or set JAVA_HOME.") from e
    # The driver leaves non-daemon helper threads behind; destroying the JVM at
    # interpreter exit would wait on them forever and the server would never quit.
    jpype.config.destroy_jvm = False
    jpype.startJVM(jvm_path, "-Xrs", classpath=[str(jar)], convertStrings=False)
    jpype.JClass(DRIVER_CLASS)  # fail early if the jar is not a Tibero driver

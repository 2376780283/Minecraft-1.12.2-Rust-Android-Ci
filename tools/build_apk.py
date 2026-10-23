#!/usr/bin/env python3
"""Build and sign the Minecraft 1.12.2 Rust Android APK.

Stages runtime/assets -> writes mcassets.list -> aapt2 link ->
zipalign -> apksigner (via java -jar). Uses the Android SDK installed on this
machine. Works on Windows and Linux (CI). Run from the repository root:
    python tools/build_apk.py
"""
import os
import pathlib
import platform
import shutil
import subprocess

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
STAGE = DIST / "stage"
ASSETS_STAGE = STAGE / "mcassets"
LIST_FILE = STAGE / "mcassets.list"

IS_WINDOWS = platform.system() == "Windows"


def find_sdk() -> pathlib.Path:
    candidates = []
    env = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if env:
        candidates.append(pathlib.Path(env))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates.append(pathlib.Path(local) / "Android" / "Sdk")
    candidates.append(pathlib.Path.home() / "Android" / "Sdk")
    for candidate in candidates:
        if (candidate / "build-tools").is_dir():
            return candidate
    raise SystemExit("Android SDK not found (set ANDROID_HOME)")


def _version_key(name: str):
    """Sort dotted version dirs (34.0.0, android-34, ndk;29) sanely."""
    parts = []
    for chunk in name.replace("-", ".").replace(";", ".").split("."):
        parts.append(int(chunk) if chunk.isdigit() else -1)
    return parts


def tool(sdk: pathlib.Path, name: str) -> pathlib.Path:
    """Resolve a build-tools binary, appending .exe only on Windows."""
    build_tools = sorted(
        (sdk / "build-tools").iterdir(), key=lambda p: _version_key(p.name), reverse=True
    )[0]
    exe_name = name + (".exe" if IS_WINDOWS else "")
    candidate = build_tools / exe_name
    if not candidate.is_file():
        alt = build_tools / name
        if alt.is_file():
            return alt
        raise SystemExit(f"missing SDK tool: {candidate}")
    return candidate


def platform_jar(sdk: pathlib.Path) -> pathlib.Path:
    platforms = sorted(
        (sdk / "platforms").iterdir(), key=lambda p: _version_key(p.name), reverse=True
    )
    if not platforms:
        raise SystemExit("no android platforms installed (sdkmanager 'platforms;android-34')")
    return platforms[0] / "android.jar"


def run(cmd):
    print("[APK]", " ".join(str(c) for c in cmd))
    subprocess.run([str(c) for c in cmd], check=True)


def stage_assets(src: pathlib.Path) -> None:
    shutil.rmtree(ASSETS_STAGE, ignore_errors=True)
    ASSETS_STAGE.mkdir(parents=True)
    entries = []
    if src.is_dir():
        for path in sorted(src.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(src).as_posix()
            # aapt2 drops dotfiles from APK assets; keep the manifest consistent.
            if any(part.startswith(".") for part in rel.split("/")):
                continue
            target = ASSETS_STAGE / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            entries.append(rel)
    LIST_FILE.write_text(("\n".join(entries) + "\n") if entries else "", encoding="utf-8")
    print(f"[APK] staged {len(entries)} asset files")


def add_native_libs(apk: pathlib.Path) -> None:
    import zipfile
    so_src = ROOT / "target" / "android" / "arm64-v8a" / "libminecraft_1_12_2_rust_vulkan.so"
    if not so_src.is_file():
        raise SystemExit("native lib not built; run cargo ndk ... first")
    with zipfile.ZipFile(apk, "a", zipfile.ZIP_DEFLATED) as archive:
        archive.write(so_src, "lib/arm64-v8a/libminecraft_1_12_2_rust_vulkan.so")
    print(f"[APK] added native lib ({so_src.stat().st_size // 1024} KiB)")

    # oboe (AAudio via cpal) links the shared C++ runtime; without this the
    # game .so fails to dlopen with "cannot locate symbol __cxa_pure_virtual".
    libcpp = find_libcpp_shared()
    if libcpp is not None:
        with zipfile.ZipFile(apk, "a", zipfile.ZIP_DEFLATED) as archive:
            archive.write(libcpp, "lib/arm64-v8a/libc++_shared.so")
        print(f"[APK] added libc++_shared.so ({libcpp.stat().st_size // 1024} KiB)")
    else:
        print("[APK] warning: libc++_shared.so not found in the Android NDK")


def find_libcpp_shared() -> pathlib.Path | None:
    """Locate libc++_shared.so in the installed NDK (arm64-v8a)."""
    sdk = find_sdk()
    ndk_dir = sdk / "ndk"
    if not ndk_dir.is_dir():
        return None
    candidates = sorted(
        list(ndk_dir.glob("*/sources/cxx-stl/llvm-libc++/libs/arm64-v8a/libc++_shared.so"))
        + list(ndk_dir.glob(
            "*/toolchains/llvm/prebuilt/*/sysroot/usr/lib/aarch64-linux-android/libc++_shared.so"
        ))
    )
    return candidates[-1] if candidates else None


def find_keytool() -> str:
    kt = shutil.which("keytool")
    if kt:
        return kt
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        candidate = pathlib.Path(java_home) / "bin" / ("keytool.exe" if IS_WINDOWS else "keytool")
        if candidate.is_file():
            return str(candidate)
    try:
        out = subprocess.run(
            ["java", "-XshowSettings:properties", "-version"],
            capture_output=True, text=True, check=True,
        ).stderr
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("java.home"):
                home = line.split("=", 1)[1].strip()
                candidate = pathlib.Path(home) / "bin" / ("keytool.exe" if IS_WINDOWS else "keytool")
                if candidate.is_file():
                    return str(candidate)
    except (OSError, subprocess.CalledProcessError):
        pass
    raise SystemExit("keytool not found; install a JDK or add it to PATH")


def debug_keystore() -> pathlib.Path:
    """Reuse the standard Android debug keystore, or generate one via keytool."""
    for candidate in (
        pathlib.Path.home() / ".android" / "debug.keystore",
        DIST / "debug.keystore",
    ):
        if candidate.is_file():
            return candidate
    keystore = DIST / "debug.keystore"
    keystore.parent.mkdir(parents=True, exist_ok=True)
    keytool = find_keytool()
    run([keytool, "-genkeypair", "-v", "-keystore", keystore, "-alias", "androiddebugkey",
         "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
         "-storepass", "android", "-keypass", "android",
         "-dname", "CN=Android Debug,O=Android,C=US"])
    return keystore


def main() -> None:
    assets_src = ROOT / "runtime" / "assets"
    if not assets_src.is_dir():
        print("[APK] note: runtime/assets not present; building an APK without assets")
    sdk = find_sdk()
    aapt2 = tool(sdk, "aapt2")
    zipalign = tool(sdk, "zipalign")
    apksigner_jar = aapt2.parent / "lib" / "apksigner.jar"
    if not apksigner_jar.is_file():
        raise SystemExit(f"apksigner.jar not found: {apksigner_jar}")
    jar = platform_jar(sdk)

    stage_assets(assets_src)
    DIST.mkdir(exist_ok=True)

    unaligned = DIST / "base.unaligned.apk"
    link_args = [aapt2, "link", "-o", unaligned, "-I", jar, "--manifest", ROOT / "AndroidManifest.xml",
                 "--min-sdk-version", "31", "--target-sdk-version", "34",
                 "--version-code", "1", "--version-name", "0.1", "-A", STAGE]
    res_dir = ROOT / "res"
    if res_dir.is_dir():
        compiled_res = DIST / "res.zip"
        run([aapt2, "compile", "--dir", res_dir, "-o", compiled_res])
        link_args += ["-R", compiled_res]
    run(link_args)

    add_native_libs(unaligned)

    aligned = DIST / "base.aligned.apk"
    run([zipalign, "-f", "4", unaligned, aligned])

    keystore = debug_keystore()

    final = DIST / "Minecraft112Rust.apk"
    run(["java", "-jar", apksigner_jar, "sign", "--ks", keystore, "--ks-pass", "pass:android",
         "--out", final, aligned])
    print(f"[APK] done: {final} ({final.stat().st_size // (1024 * 1024)} MiB)")


if __name__ == "__main__":
    main()

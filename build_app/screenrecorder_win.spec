# PyInstaller spec — builds ScreenRecorder.exe, a single-file Windows background
# agent. Mirrors screenrecorder.spec (macOS) but targets a console-less Windows
# onefile binary with a bundled ffmpeg.exe. Built by the windows-build.yml CI on
# a windows-latest runner (PyInstaller cannot cross-compile from macOS).
import os
from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for pkg in ("googleapiclient", "google_auth_httplib2", "google.auth",
            "google_auth_oauthlib", "google", "cryptography",
            "pynput", "mss", "PIL"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

hiddenimports += [
    "screenrecord.recorder", "screenrecord.uploader", "screenrecord.heartbeat",
    "screenrecord.encryption", "screenrecord.compliance", "screenrecord.updater",
    "screenrecord.sheets_backend", "screenrecord.config_manager",
    "screenrecord.platform_utils", "screenrecord.input_monitor",
    "screenrecord.diagnostics", "screenrecord.provision",
    "screenrecord.tray", "screenrecord.macos_permissions",
    "yaml", "psutil",
    # pynput/mss pick their OS backend at runtime; PyInstaller's static analysis
    # misses these, so name them explicitly or input capture silently no-ops.
    "pynput.keyboard._win32", "pynput.mouse._win32", "pynput._util.win32",
    "mss.windows",
    # pystray picks its backend at runtime too -> name the Windows one.
    "pystray._win32",
]

# Bundle ffmpeg.exe — the CI step downloads a static Windows build and drops it
# next to this spec before PyInstaller runs.
_ffmpeg = os.path.join(os.path.dirname(os.path.abspath(SPEC)), "ffmpeg.exe")
binaries += [(_ffmpeg, ".")]

# Bake deployment values so the frozen exe can self-provision/repair
# %USERPROFILE%\\.screenrecord even when an MDM installer script did not run in
# the target user's context.
datas += [("_provision.json", ".")]

a = Analysis(
    ["../screenrecord/app_entry.py"],
    pathex=[".."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=[
        "google.generativeai", "google_genai", "openai", "httpx",
        "chromadb", "sentence_transformers", "torch", "transformers",
        "flask", "screenrecord.analyzer", "screenrecord.rag_system",
        "screenrecord.dashboard", "tkinter", "matplotlib",
        # NOTE: do not exclude PIL — input_monitor needs Pillow (PIL._imaging).
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

# Single-file build: include binaries + datas in the EXE and omit COLLECT.
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="ScreenRecorder",
    console=False,                 # background agent, no console window
    disable_windowed_traceback=True,
    upx=False,
)

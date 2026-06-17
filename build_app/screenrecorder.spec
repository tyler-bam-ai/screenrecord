# PyInstaller spec — builds ScreenRecorder.app (background agent).
# Bundles the screenrecord service, its core deps, and a static ffmpeg.
# Heavy optional deps (analysis/RAG/ML) are excluded; they're imported lazily
# behind try/except in main.py and stay disabled in the deployed config.

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []
for pkg in ("googleapiclient", "google_auth_httplib2", "google.auth",
            "google_auth_oauthlib", "google", "cryptography",
            "pynput", "mss", "PIL"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hiddenimports += h

# Service submodules that are imported lazily at runtime.
hiddenimports += [
    "screenrecord.recorder", "screenrecord.uploader", "screenrecord.heartbeat",
    "screenrecord.encryption", "screenrecord.compliance", "screenrecord.updater",
    "screenrecord.sheets_backend", "screenrecord.config_manager",
    "screenrecord.platform_utils", "screenrecord.input_monitor",
    "yaml", "psutil",
]

# Bundle the static ffmpeg next to the executable (Contents/MacOS).
binaries += [("bin/ffmpeg", ".")]

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
        "screenrecord.dashboard", "tkinter", "matplotlib", "PIL",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz, a.scripts, [], exclude_binaries=True,
    name="ScreenRecorder",
    console=False, disable_windowed_traceback=True,
    target_arch='universal2', codesign_identity=None, entitlements_file=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="ScreenRecorder")

app = BUNDLE(
    coll,
    name="ScreenRecorder.app",
    icon=None,
    bundle_identifier="ai.bam.screenrecord",
    info_plist={
        "CFBundleName": "ScreenRecorder",
        "CFBundleDisplayName": "Screen Recorder",
        "CFBundleShortVersionString": "1.1.1",
        "CFBundleVersion": "3",
        "LSUIElement": True,          # background agent, no Dock icon
        "LSBackgroundOnly": False,    # still needs a UI session for the TCC prompt
        "NSScreenCaptureUsageDescription":
            "Screen Recorder captures this Mac's screen for authorized workplace monitoring.",
        "LSMinimumSystemVersion": "11.0",
    },
)

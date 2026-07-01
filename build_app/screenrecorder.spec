# PyInstaller spec — builds ScreenRecorder.app (background agent).
# Bundles the screenrecord service, its core deps, and a static ffmpeg.
# Heavy optional deps (analysis/RAG/ML) are excluded; they're imported lazily
# behind try/except in main.py and stay disabled in the deployed config.

from PyInstaller.utils.hooks import collect_all

_version_ns = {}
exec(open("../screenrecord/version.py", encoding="utf-8").read(), _version_ns)

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
    "screenrecord.provision", "screenrecord.tray",
    "screenrecord.macos_permissions", "screenrecord.diagnostics",
    "screenrecord.release_updater", "screenrecord.version",
    "yaml", "psutil",
    # pynput/mss pick their OS backend at runtime; PyInstaller's static analysis
    # misses these, so name them explicitly or input capture silently no-ops.
    "pynput.keyboard._darwin", "pynput.mouse._darwin", "pynput._util.darwin",
    "mss.darwin",
]

# Bundle the static ffmpeg next to the executable (Contents/MacOS).
binaries += [("bin/ffmpeg", ".")]

# Bake the deployment values so the agent can self-provision its config on first
# login. build_app.sh writes _provision.json from bootstrap.sh before this runs.
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
        "CFBundleShortVersionString": _version_ns["MAC_VERSION"],
        "CFBundleVersion": _version_ns["MAC_BUILD"],
        "LSUIElement": True,          # background agent, no Dock icon
        "LSBackgroundOnly": False,    # still needs a UI session for the TCC prompt
        "NSScreenCaptureUsageDescription":
            "Screen Recorder captures this Mac's screen for authorized workplace monitoring.",
        "LSMinimumSystemVersion": "11.0",
    },
)

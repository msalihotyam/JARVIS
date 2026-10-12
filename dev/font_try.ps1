# DFFONTTRY: start JARVIS from THIS checkout with one font render mode, on throwaway data.
#
#   .\dev\font_try.ps1 -Mode native   # today's rendering (DirectWrite ClearType)
#   .\dev\font_try.ps1 -Mode O        # FreeType + vertical hinting (DFFONTLAB2 O)
#   .\dev\font_try.ps1 -Mode T        # O + stem darkening (DFFONTLAB2 T)
#
# Every run gets a NEW folder under %TEMP%\jarvis-font-try: its own home (config.json,
# ui-preferences.json) and its own database with a small seeded sample chat. The real
# ~\.jarvis and data\jarvis.db are never read or written. Execution is switched off in
# the throwaway config, so nothing typed in this window can start a model call.
# The mode reaches only the started process (jarvis.ui.app FONT_RENDER_ENV); this
# shell's own environment is left as it was.
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('native', 'O', 'T')]
    [string]$Mode
)
$ErrorActionPreference = 'Stop'
$Mode = @{ native = 'native'; o = 'O'; t = 'T' }[$Mode.ToLowerInvariant()]   # ValidateSet ignores case
$repo = Split-Path -Parent $PSScriptRoot

# One JARVIS at a time: a second launch would only bring the running one to the front.
$running = @(Get-CimInstance Win32_Process | Where-Object {
    $_.Name -eq 'JARVIS.exe' -or
    ($_.Name -match '^pythonw?\.exe$' -and $_.CommandLine -match '-m\s+jarvis\.ui\b|launch_jarvis\.py')
})
if ($running.Count -gt 0) {
    Write-Host ("JARVIS is already running (PID " + (($running | ForEach-Object { $_.ProcessId }) -join ', ') + ").")
    Write-Host "Quit it from the tray icon (right click > Quit JARVIS), then run this again."
    exit 1
}

$temp = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$root = [IO.Path]::GetFullPath((Join-Path $temp ('jarvis-font-try\' + $Mode + '-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))))
$db = [IO.Path]::GetFullPath((Join-Path $root 'data\jarvis.db'))
$live = [IO.Path]::GetFullPath((Join-Path $repo 'data\jarvis.db'))
# Fail closed: the database must be inside the new %TEMP% folder, never the checkout's data\.
if (-not $db.StartsWith($root + '\', [StringComparison]::OrdinalIgnoreCase) -or
    -not $root.StartsWith($temp, [StringComparison]::OrdinalIgnoreCase) -or
    $db -ieq $live) {
    Write-Host "REFUSED: the throwaway database would not be under %TEMP% ($db)."
    exit 1
}
New-Item -ItemType Directory -Force -Path (Join-Path $root 'home'), (Join-Path $root 'data') | Out-Null

function Start-Python([string[]]$arguments) {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = 'python'
    $psi.Arguments = ($arguments | ForEach-Object { '"' + $_ + '"' }) -join ' '
    $psi.WorkingDirectory = $repo
    $psi.UseShellExecute = $false
    $env_ = $psi.EnvironmentVariables
    $env_['USERPROFILE'] = (Join-Path $root 'home')     # Path.home(): config.json and ui-preferences.json
    $env_['PYTHONPATH'] = $repo
    $env_['JARVIS_FONT_RENDER'] = $Mode
    foreach ($name in 'QT_QPA_PLATFORM', 'FREETYPE_PROPERTIES', 'QT_SCALE_FACTOR', 'TOMORROW_API_KEY') {
        $env_.Remove($name)                             # the mode alone decides; no weather network call
    }
    $process = [System.Diagnostics.Process]::Start($psi)
    $process.WaitForExit()
    return $process.ExitCode
}

# The seed: the throwaway config (database + execution off), then the sample chat.
$seed = @'
import sys
from pathlib import Path

TITLE = "Yaz\u0131 tipi denemesi / Font check"
OTHERS = ("Haftal\u0131k al\u0131\u015fveri\u015f listesi", "\u00c7ar\u015famba toplant\u0131 notlar\u0131",
          "Weekly review notes", "Python beti\u011findeki hata")
CODE = (
    "```python\n"
    "def total(invoices: list[float]) -> float:\n"
    "    \"\"\"Sum of the week's invoices, in TRY.\"\"\"\n"
    "    return round(sum(invoices), 2)\n"
    "\n"
    "print(total([2450.0, 1890.5, 3120.0, 2379.5]))  # 9840.0\n"
    "```"
)
TURNS = (
    ("user", "Ge\u00e7en haftaki faturalar\u0131n toplam\u0131n\u0131 hat\u0131rlat\u0131r m\u0131s\u0131n? "
             "\u015eu \u00fc\u00e7 kalem \u00f6nemli: elektrik, su ve do\u011falgaz."),
    ("assistant", "Ge\u00e7en haftaki d\u00f6rt faturan\u0131n toplam\u0131 **9.840 \u20ba**: \u00fc\u00e7\u00fc "
                  "elektrik, su ve do\u011falgaz, biri iskele kiras\u0131. \u00d6deme tarihi en yak\u0131n olan "
                  "do\u011falgaz faturas\u0131; son g\u00fcn Cuma.\n\n"
                  "- Elektrik: 2.450 \u20ba\n- Su: 1.890,50 \u20ba\n- Do\u011falgaz: 3.120 \u20ba\n"
                  "- \u0130skele kiras\u0131: 2.379,50 \u20ba\n\n"
                  "Hesab\u0131 kendin de kontrol etmek istersen:\n\n" + CODE),
    ("user", "Thanks. Can you write a short summary in English for the team, too?"),
    ("assistant", "Sure. Here is a short summary you can paste as it is:\n\n"
                  "Last week we received four invoices totalling 9,840 TRY. Three are utilities "
                  "(electricity, water and natural gas) and one is the scaffolding rent. The natural gas "
                  "invoice is due first, on Friday, so it should be paid before the others; the "
                  "remaining three can wait until the end of the month without any late fee. If the "
                  "scaffolding stays up for another week, the rent will rise by roughly a quarter, so "
                  "it is worth deciding by Wednesday whether the facade work can finish on time. "
                  "Quick brown fox jumps over the lazy dog: 0123456789 Il1| O0 rn m."),
)


def seed(store):
    """The sample chat, newest so it opens first, plus a few sidebar rows (a row needs a turn)."""
    from jarvis.conversation import append_turn

    def say(conversation_id, n, role, text):
        store.update_conversation_state(conversation_id, lambda state: append_turn(
            state, role=role, kind="user" if role == "user" else "reply", text=text,
            message=text if role == "user" else "", key=f"font-try-{n}")[0])

    for title in OTHERS:
        other = store.create_conversation().id
        store.set_conversation_title(other, title)
        say(other, 0, "user", title)
    chat = store.create_conversation().id
    store.set_conversation_title(chat, TITLE)
    for n, (role, text) in enumerate(TURNS):
        say(chat, n, role, text)
    return chat


if __name__ == "__main__":
    root = Path(sys.argv[1]).resolve()
    db = root / "data" / "jarvis.db"
    from jarvis.settings import PROJECT_ROOT, ConfigStatus, ConfigStore, Settings, load_settings
    from jarvis.ui.backend import Backend
    from jarvis.ui.preferences import UiPreferences

    # Fail closed before anything is opened: the home is the throwaway one, and the
    # database it names is inside it and is not the checkout's data\jarvis.db.
    if Path.home().resolve() != root / "home":
        sys.exit("REFUSED: the throwaway home did not take (Path.home() is not under the run folder).")
    ConfigStore().write(Settings(memory_database=db, execution_enabled=False))
    loaded = load_settings()
    named = loaded.settings.memory_database.resolve()
    if loaded.status is not ConfigStatus.VALID or named != db or root not in named.parents \
            or named == (PROJECT_ROOT / "data" / "jarvis.db").resolve():
        sys.exit("REFUSED: the throwaway config does not name the throwaway database.")
    prefs = UiPreferences()
    prefs.data["language"] = "tr-TR"
    prefs.save()
    seed(Backend().conversations())
    print("seeded " + str(db))
'@
[IO.File]::WriteAllText((Join-Path $root 'seed.py'), $seed, (New-Object Text.UTF8Encoding $false))
if ((Start-Python @((Join-Path $root 'seed.py'), $root)) -ne 0) {
    Write-Host "The throwaway data could not be prepared; JARVIS was not started."
    exit 1
}

Write-Host ""
Write-Host "JARVIS font render: $Mode"
Write-Host "  data (throwaway): $root"
Write-Host "  close it from the tray (Quit JARVIS) before the next mode."
exit (Start-Python @('-m', 'jarvis.ui'))

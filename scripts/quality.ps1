param(
    [switch]$Audit
)

# Araç sürümlerinin tek doğruluk kaynağı. Baseline sayıları bu sürümlere bağlıdır.
$RuffVersion = "0.16.0"
$BanditVersion = "1.9.4"

# Araçlar uvx ile izole ve sabit sürümde çalışır; projeye kurulmaz,
# runtime bağımlılığı eklemez. Format ve otomatik düzeltme kullanılmaz.
$ProjectRoot = Split-Path -Parent $PSScriptRoot

Push-Location $ProjectRoot
try {
    if ($Audit) {
        # Non-blocking tam denetim: --exit-zero yalnız bulguları yutar,
        # araç/çalıştırma hatası yine de yükselir.
        Write-Output "=== Ruff full audit ==="
        & uvx "ruff@$RuffVersion" check jarvis tests --isolated --target-version py311 --no-fix --no-cache --output-format=concise --exit-zero
        if ($LASTEXITCODE -ne 0) {
            exit $LASTEXITCODE
        }

        Write-Output "=== Bandit full audit ==="
        & uvx "bandit@$BanditVersion" -r jarvis -f screen --exit-zero
        exit $LASTEXITCODE
    }

    # Blocking: yalnız temel hata kuralları.
    & uvx "ruff@$RuffVersion" check jarvis tests --isolated --target-version py311 --select E4,E7,E9,F --no-fix --no-cache --output-format=concise
    if ($LASTEXITCODE -ne 0) {
        exit $LASTEXITCODE
    }

    # Blocking: yalnız Medium/High. Low bulgular gizlenmez, eşiğin altındadır.
    & uvx "bandit@$BanditVersion" -r jarvis -q --severity-level medium
    exit $LASTEXITCODE
} finally {
    Pop-Location
}

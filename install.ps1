# Install the haystack-enterprise CLI on Windows (PowerShell):
#   powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/deepset-ai/haystack-enterprise-sdk/main/install.ps1 | iex"
#
# Optional environment variables:
#   HE_VERSION  install a specific version, e.g. $env:HE_VERSION = "0.2.0"
#   HE_EXTRAS   install extras, e.g. $env:HE_EXTRAS = "[deploy]"
$ErrorActionPreference = "Stop"

$Package = "haystack-enterprise-sdk$env:HE_EXTRAS"
if ($env:HE_VERSION) { $Package += "==$env:HE_VERSION" }
$DocsUrl = "https://docs.cloud.deepset.ai/reference/sdk-overview"

Write-Host @'
🥁 Hello and welcome! Now installing the...
  _   _                 _             _
 | | | | __ _ _   _ ___| |_ __ _  ___| | __
 | |_| |/ _` | | | / __| __/ _` |/ __| |/ /
 |  _  | (_| | |_| \__ \ || (_| | (__|   <
 |_| |_|\__,_|\__, |___/\__\__,_|\___|_|\_\
              |___/        E N T E R P R I S E   C L I

'@

Write-Host "🔍 Checking for uv..."
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "💾 uv not found. Installing it from https://astral.sh/uv ..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$HOME\.local\bin;$env:Path"
}
Write-Host "✅ Using $(uv --version)"

Write-Host "📦 Installing $Package..."
uv tool install --quiet --upgrade --python 3.12 $Package
if ($LASTEXITCODE -ne 0) { throw "uv tool install failed with exit code $LASTEXITCODE" }

Write-Host "🔗 Making sure the uv tool directory is on your PATH..."
uv tool update-shell *> $null

$BinDir = uv tool dir --bin
$Version = & (Join-Path $BinDir "haystack-enterprise.exe") --version

Write-Host @"

📺 Success! $Version is now installed!
🔐 Next, log in with:       haystack-enterprise login
🚀 Deploy a pipeline with:  haystack-enterprise deploy pipeline.py SERVICE
📚 Docs:                    $DocsUrl
"@

if (-not (Get-Command haystack-enterprise -ErrorAction SilentlyContinue)) {
    Write-Host ""
    Write-Host "↻  Open a new terminal (or add $BinDir to your PATH) to use haystack-enterprise."
}

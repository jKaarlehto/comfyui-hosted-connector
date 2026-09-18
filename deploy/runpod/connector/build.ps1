param(
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [string]$SiteUrl = '',
    [string]$Version = '1.1.0.0',
    [string]$CertificateThumbprint = '',
    [string]$Publisher = 'CN=ComfyUI-Notch',
    [switch]$Package,
    [switch]$Test
)
$ErrorActionPreference = 'Stop'
if ($SiteUrl) {
    $site = [Uri]$SiteUrl
    if (!$site.IsAbsoluteUri -or $site.Scheme -ne 'https' -or $site.UserInfo -or $site.Query -or $site.Fragment) {
        throw 'SiteUrl must be an HTTPS origin/path without credentials, query, or fragment.'
    }
}
if ($Version -notmatch '^\d+\.\d+\.\d+\.\d+$') { throw 'Version must have four numeric components.' }
$output = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $output | Out-Null
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (!(Test-Path -LiteralPath $csc)) { throw 'The .NET Framework 4 compiler is required.' }
$siteFile = Join-Path $output 'site.txt'
[IO.File]::WriteAllText($siteFile, $SiteUrl.TrimEnd('/'), (New-Object Text.UTF8Encoding($false)))
$launcher = Join-Path (Split-Path $PSScriptRoot -Parent) 'start_hosted_comfyui.ps1'
$access = Join-Path (Split-Path $PSScriptRoot -Parent) 'hosted_access.ps1'
$exe = Join-Path $output 'HostedComfyUIConnector.exe'
$sources = @((Join-Path $PSScriptRoot 'Connector.cs'), (Join-Path $PSScriptRoot 'Presence.cs'), (Join-Path $PSScriptRoot 'LiveStatus.cs'), (Join-Path $PSScriptRoot 'Workspace.cs'))
$compilerArgs = @('/nologo', '/optimize+', '/platform:x64', '/reference:System.dll', '/reference:System.Core.dll',
    '/reference:System.Drawing.dll', '/reference:System.Windows.Forms.dll', '/reference:System.Web.Extensions.dll',
    "/win32manifest:$(Join-Path $PSScriptRoot 'Connector.manifest')",
    "/resource:$launcher,launcher.ps1", "/resource:$access,access.ps1", "/resource:$siteFile,site.txt")
& $csc @compilerArgs /target:winexe "/out:$exe" @sources
if ($LASTEXITCODE -ne 0) { throw 'Connector compilation failed.' }
if ($Test) {
    $testExe = Join-Path $output 'ConnectorTests.exe'
    & $csc @compilerArgs /target:exe /main:ConnectorTests "/out:$testExe" @sources (Join-Path $PSScriptRoot 'ConnectorTests.cs')
    if ($LASTEXITCODE -ne 0) { throw 'Connector test compilation failed.' }
    & $testExe
    if ($LASTEXITCODE -ne 0) { throw 'Connector tests failed.' }
    $tokens = $null; $errors = $null
    [Management.Automation.Language.Parser]::ParseFile($launcher, [ref]$tokens, [ref]$errors) | Out-Null
    if ($errors.Count) { throw ($errors | Out-String) }
    & (Join-Path $PSScriptRoot 'ProgressTests.ps1')
    & (Join-Path $PSScriptRoot 'AccessTests.ps1')
}
$sdkRoot = Join-Path ${env:ProgramFiles(x86)} 'Windows Kits\10\bin'
$sdk = Get-ChildItem -LiteralPath $sdkRoot -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Where-Object { Test-Path (Join-Path $_.FullName 'x64\makeappx.exe') } | Select-Object -First 1
if ($CertificateThumbprint) {
    if (!$sdk) { throw 'Windows SDK signing tools are required.' }
    $certificate = Get-Item -LiteralPath "Cert:\CurrentUser\My\$CertificateThumbprint"
    if (!$certificate.HasPrivateKey) { throw 'The signing certificate has no private key.' }
    $Publisher = $certificate.Subject
    & (Join-Path $sdk.FullName 'x64\signtool.exe') sign /sha1 $CertificateThumbprint /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $exe
    if ($LASTEXITCODE -ne 0) { throw 'Executable signing failed.' }
}
if ($Package) {
    if (!$sdk) { throw 'Windows SDK packaging tools are required.' }
    $layout = Join-Path $output 'package'
    $assets = Join-Path $layout 'Assets'
    New-Item -ItemType Directory -Force -Path $assets | Out-Null
    Copy-Item -LiteralPath $exe -Destination $layout -Force
    $xmlPublisher = [Security.SecurityElement]::Escape($Publisher)
    $manifest = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'AppxManifest.xml') -Raw).Replace('@PUBLISHER@', $xmlPublisher).Replace('@VERSION@', $Version)
    [IO.File]::WriteAllText((Join-Path $layout 'AppxManifest.xml'), $manifest, (New-Object Text.UTF8Encoding($false)))
    Add-Type -AssemblyName System.Drawing
    foreach ($item in @(@('StoreLogo.png', 50), @('Square44x44Logo.png', 44), @('Square150x150Logo.png', 150))) {
        $bitmap = New-Object Drawing.Bitmap($item[1], $item[1])
        $graphics = [Drawing.Graphics]::FromImage($bitmap)
        $graphics.Clear([Drawing.Color]::FromArgb(32, 32, 32))
        $pen = New-Object Drawing.Pen([Drawing.Color]::FromArgb(104, 173, 245), ($item[1] / 12.0))
        $graphics.DrawEllipse($pen, ($item[1] / 5.0), ($item[1] / 5.0), ($item[1] * 0.6), ($item[1] * 0.6))
        $bitmap.Save((Join-Path $assets $item[0]), [Drawing.Imaging.ImageFormat]::Png)
        $pen.Dispose(); $graphics.Dispose(); $bitmap.Dispose()
    }
    $msix = Join-Path $output 'HostedComfyUI.msix'
    & (Join-Path $sdk.FullName 'x64\makeappx.exe') pack /d $layout /p $msix /o
    if ($LASTEXITCODE -ne 0) { throw 'MSIX packaging failed.' }
    if ($CertificateThumbprint) {
        & (Join-Path $sdk.FullName 'x64\signtool.exe') sign /sha1 $CertificateThumbprint /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $msix
        if ($LASTEXITCODE -ne 0) { throw 'MSIX signing failed.' }
        if (!$SiteUrl) { throw 'SiteUrl is required to publish an App Installer file.' }
        $appInstaller = (Get-Content -LiteralPath (Join-Path $PSScriptRoot 'HostedComfyUI.appinstaller') -Raw).Replace('@BASE_URL@', [Security.SecurityElement]::Escape($SiteUrl.TrimEnd('/') + '/downloads')).Replace('@PUBLISHER@', $xmlPublisher).Replace('@VERSION@', $Version)
        [IO.File]::WriteAllText((Join-Path $output 'HostedComfyUI.appinstaller'), $appInstaller, (New-Object Text.UTF8Encoding($false)))
    } else { Write-Host 'Unsigned MSIX built for validation only. Publish the executable fallback; App Installer requires trusted signing.' }
}
Write-Host ("Built {0} ({1:N0} bytes)" -f $exe, (Get-Item -LiteralPath $exe).Length)

param(
    [string]$StateDir = '',
    [string]$EnvFile = ''
)

$ErrorActionPreference = 'Stop'
$script:RepositoryRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
if (!$StateDir) { $StateDir = Join-Path $script:RepositoryRoot '.runpod' }
if (!$EnvFile) { $EnvFile = Join-Path $script:RepositoryRoot '.env' }
$script:StateDir = [IO.Path]::GetFullPath($StateDir)
$script:EnvFile = [IO.Path]::GetFullPath($EnvFile)
$script:Python = $null
$script:PythonArguments = @()
$script:Tunnel = $null

function Show-OwnerMenu {
    Write-Host 'Hosted ComfyUI - Owner' -ForegroundColor Cyan
    Write-Host 'Needs Python 3.10+, GitHub CLI and Windows OpenSSH.'
    Write-Host 'Setup uses your GitHub CLI login for the private plugin repository.'
    Write-Host "Owner credentials: $script:EnvFile"
    Write-Host "Deployment state:  $script:StateDir"
    Write-Host 'First time: 1 setup, 2 deploy, 5 connect, 7 starter, 8 invite.'
    try {
        while ($true) {
            Write-Host ''
            Write-Host ' 1  Setup / update template and secrets'
            Write-Host ' 2  Deploy a new Pod'
            Write-Host ' 3  Show status'
            Write-Host ' 4  Start Pod'
            Write-Host ' 5  Connect / open WebUI'
            Write-Host ' 6  Stop Pod (flush storage first)'
            Write-Host ' 7  Setup / update tester starter'
            Write-Host ' 8  Create / refresh tester invitation'
            Write-Host ' 9  Revoke tester invitation'
            Write-Host ' L  Copy a private invitation link'
            Write-Host ' I  List invited testers'
            Write-Host ' G  Sign in to GitHub'
            Write-Host ' 0  Exit and close owner tunnel'
            $choice = Read-Host 'Choose'
            if ($choice -eq '0') { break }
            try {
                Invoke-OwnerAction $choice
            } catch {
                Write-Host "Failed: $($_.Exception.Message)" -ForegroundColor Red
                Write-Host 'No further steps were run. Correct the issue and choose the action again.'
            }
            [void](Read-Host 'Press Enter to return to the menu')
        }
    } finally {
        Close-OwnerTunnel
    }
}

function Invoke-OwnerAction([string]$Choice) {
    switch ($Choice.ToUpperInvariant()) {
        '1' {
            Require-Command 'gh.exe' 'Install GitHub CLI, then choose G to sign in.'
            Require-Command 'ssh-keygen.exe' 'Install the Windows OpenSSH client.'
            Write-Host 'Using saved setup settings, or A40 / EU-SE-1 / 32 GB disk / global storage.'
            Write-Host 'The Runpod key is requested privately if missing, then saved to the owner .env.'
            Invoke-OwnerHelper 'runpod.py' @('setup')
        }
        '2' {
            Write-Host 'Deploying starts GPU billing. Saved template and storage settings will be used.'
            if ((Read-Host 'Deploy now? [y/N]') -eq 'y') { Invoke-OwnerHelper 'runpod.py' @('deploy') }
        }
        '3' { Invoke-OwnerHelper 'runpod.py' @('status') }
        '4' { Invoke-OwnerHelper 'runpod.py' @('start') }
        '5' { Connect-Owner }
        '6' {
            Invoke-OwnerHelper 'runpod.py' @('stop')
            Close-OwnerTunnel
        }
        '7' {
            Write-Host 'This applies the hosted template. Runpod may restart the Pod.'
            if ((Read-Host 'Apply now? [y/N]') -eq 'y') { Invoke-OwnerHelper 'hosted.py' @('setup') }
        }
        '8' {
            $guest = Read-GuestName
            Write-Host 'The Pod must be running. Creating a private invitation package...'
            Invoke-OwnerHelper 'hosted.py' @('share', '--guest', $guest)
            $folder = Join-Path $script:StateDir "shares/$guest"
            Write-Host "Send only the two tester launcher files and invitation.txt from: $folder"
            Write-Host 'The invitation is private to this tester. Do not share the owner .env or state.'
            Start-Process -FilePath explorer.exe -ArgumentList ('"' + $folder + '"') | Out-Null
        }
        '9' {
            $guest = Read-GuestName
            Write-Host 'The Pod must be running. Existing tunnels last until closed or the Pod stops.'
            if ((Read-Host "Revoke $guest? [y/N]") -eq 'y') {
                Invoke-OwnerHelper 'hosted.py' @('revoke', '--guest', $guest)
            }
        }
        'G' {
            Require-Command 'gh.exe' 'Install GitHub CLI first: https://cli.github.com/'
            & gh.exe auth login --hostname github.com --web
            if ($LASTEXITCODE -ne 0) { throw 'GitHub sign-in did not finish.' }
        }
        'L' {
            $guest = Read-GuestName
            $site = Read-Host 'HTTPS invitation page URL (Enter to reuse saved URL)'
            $arguments = @('link', '--guest', $guest)
            if ($site.Trim()) { $arguments += @('--site-url', $site.Trim()) }
            Invoke-OwnerHelper 'hosted.py' $arguments
            $link = Get-Content -LiteralPath (Join-Path $script:StateDir "shares/$guest/invitation-link.txt") -Raw
            Set-Clipboard -Value $link.Trim()
            Write-Host 'Private invitation link copied. Send it only to this tester.' -ForegroundColor Green
        }
        'I' { Invoke-OwnerHelper 'hosted.py' @('list') }
        default { Write-Host 'Choose one of the listed actions.' }
    }
}

function Invoke-OwnerHelper([string]$Helper, [string[]]$Arguments) {
    Resolve-OwnerPython
    $command = @($script:PythonArguments) + @((Join-Path $PSScriptRoot $Helper)) + $Arguments + @(
        '--state-dir', $script:StateDir, '--env-file', $script:EnvFile
    )
    Write-Host "Running $Helper $($Arguments -join ' ')..." -ForegroundColor Cyan
    Push-Location $script:RepositoryRoot
    try {
        & $script:Python @command | Out-Host
        if ($LASTEXITCODE -ne 0) { throw "$Helper failed (exit $LASTEXITCODE). See the message above." }
    } finally {
        Pop-Location
    }
    Write-Host 'Done.' -ForegroundColor Green
}

function Resolve-OwnerPython {
    if ($script:Python) { return }
    $candidates = @((Join-Path $script:RepositoryRoot '.venv/Scripts/python.exe'), 'py.exe', 'python.exe')
    foreach ($candidate in $candidates) {
        $found = Get-Command $candidate -ErrorAction SilentlyContinue
        if (!$found) { continue }
        $prefix = @()
        if ($candidate -eq 'py.exe') { $prefix = @('-3') }
        & $found.Source @prefix -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>$null
        if ($LASTEXITCODE -eq 0) {
            $script:Python = $found.Source
            $script:PythonArguments = $prefix
            return
        }
    }
    throw 'Install Python 3.10 or newer from python.org, then reopen this menu.'
}

function Connect-Owner {
    Require-Command 'ssh.exe' 'Install the Windows OpenSSH client.'
    $stateFile = Join-Path $script:StateDir 'state.json'
    if (!(Test-Path -LiteralPath $stateFile)) { throw 'Run setup and deploy a Pod first.' }
    $state = Get-Content -LiteralPath $stateFile -Raw | ConvertFrom-Json
    $port = 18188
    if ($state.config.local_port) { $port = [int]$state.config.local_port }
    if (!$script:Tunnel -or $script:Tunnel.HasExited) {
        Invoke-OwnerHelper 'runpod.py' @('connect', '--start', '--background')
        $tunnelId = [int](Get-Content -LiteralPath (Join-Path $script:StateDir 'tunnel-pid.txt') -Raw)
        $script:Tunnel = Get-Process -Id $tunnelId -ErrorAction Stop
    }
    $url = "http://127.0.0.1:$port"
    Write-Host "Connected: $url" -ForegroundColor Green
    Write-Host "Notch: host 127.0.0.1, port $port, transport HTTP. Keep this owner menu open."
    Start-Process $url | Out-Null
}

function Close-OwnerTunnel {
    if ($script:Tunnel -and !$script:Tunnel.HasExited) { $script:Tunnel.Kill() }
    $script:Tunnel = $null
}

function Read-GuestName {
    $guest = (Read-Host 'Tester name (letters, numbers, underscore or hyphen)').Trim()
    if ($guest -notmatch '^[a-zA-Z0-9_-]{1,48}$') {
        throw 'Use 1-48 letters, numbers, underscores or hyphens for the tester name.'
    }
    return $guest
}

function Require-Command([string]$Name, [string]$Help) {
    if (!(Get-Command $Name -ErrorAction SilentlyContinue)) { throw $Help }
}

if ($MyInvocation.InvocationName -ne '.') { Show-OwnerMenu }

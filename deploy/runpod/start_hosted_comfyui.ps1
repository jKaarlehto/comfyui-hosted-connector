param(
    [int]$LocalPort = 18188,
    [int]$StartupTimeoutMinutes = 15,
    [string]$AccessFile = "",
    [string]$EnvFile = (Join-Path $PSScriptRoot '.env'),
    [switch]$ConnectorMode,
    [string]$StopFile = "",
    [string]$SessionDirectory = "",
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$tunnel = $null
$work = $null
$storageProgress = $null

function Write-State([string]$Message) {
    Write-Host ("[{0:HH:mm:ss}] {1}" -f (Get-Date), $Message) -ForegroundColor Cyan
}

function Wait-Connection([int]$Seconds) {
    for ($i = 0; $i -lt $Seconds; $i++) {
        if ($StopFile -and (Test-Path -LiteralPath $StopFile)) { throw [OperationCanceledException]::new('Connection closed.') }
        Start-Sleep -Seconds 1
    }
}

function New-StorageProgress([string]$Url) {
    Add-Type -AssemblyName System.Net.Http
    $handler = New-Object Net.Http.HttpClientHandler
    $handler.AllowAutoRedirect = $false
    $handler.UseProxy = $false
    $client = New-Object Net.Http.HttpClient($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(3)
    $client.MaxResponseContentBufferSize = 8192
    return @{Client = $client; Url = "$Url/hosted_comfyui/storage"; Task = $null;
        NextPoll = [DateTime]::MinValue; LastState = ''; Unsupported = $false}
}

function Get-StorageProgress($Poller) {
    if ($Poller.Unsupported) { return }
    if ($Poller.Task) {
        if (!$Poller.Task.IsCompleted) { return }
        $response = $null
        try {
            $response = $Poller.Task.GetAwaiter().GetResult()
            if ([int]$response.StatusCode -eq 404) { $Poller.Unsupported = $true; return }
            if (!$response.IsSuccessStatusCode) { return }
            $value = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult() | ConvertFrom-Json
            if ($value.phase -notin @('idle', 'downloading', 'verifying', 'error') -or
                $value.filename -isnot [string] -or $value.filename.Length -gt 1024 -or
                [string]$value.completed_bytes -notmatch '^\d{1,18}$' -or
                [string]$value.total_bytes -notmatch '^\d{1,18}$') { return }
            $completed = [long]$value.completed_bytes
            $total = [long]$value.total_bytes
            if ($total -gt 0 -and $completed -gt $total) { return }
            $filename = $value.filename -replace '[\x00-\x1f\x7f]', ' '
            $errorText = if ($value.error -is [string]) { $value.error -replace '[\x00-\x1f\x7f]', ' ' } else { '' }
            if ($errorText.Length -gt 400) { $errorText = $errorText.Substring(0, 400) }
            $percent = if ($total -gt 0) { [int][Math]::Floor(100.0 * $completed / $total) } else { -1 }
            $amount = if ($total -gt 0) { $percent } else { [long][Math]::Floor($completed / 1MB) }
            $key = "$($value.phase)|$filename|$amount|$errorText"
            if ($key -eq $Poller.LastState) { return }
            $Poller.LastState = $key
            return [pscustomobject]@{phase = $value.phase; filename = $filename;
                completed_bytes = $completed; total_bytes = $total; error = $errorText}
        } catch { } finally {
            if ($response) { $response.Dispose() }
            $Poller.Task = $null
            $Poller.NextPoll = (Get-Date).AddSeconds(1)
        }
    } elseif ((Get-Date) -ge $Poller.NextPoll) {
        $Poller.Task = $Poller.Client.GetAsync($Poller.Url)
    }
}

function Format-StorageProgress($Value) {
    if ($Value.phase -eq 'idle') { return '' }
    if ($Value.phase -eq 'error') { return "Model download failed: $($Value.filename)`r`n$($Value.error)" }
    if ($Value.phase -eq 'verifying') { return "Verifying model: $($Value.filename)" }
    $done = '{0:N1} MB' -f ($Value.completed_bytes / 1MB)
    if ($Value.total_bytes -gt 0) {
        $percent = [int][Math]::Floor(100.0 * $Value.completed_bytes / $Value.total_bytes)
        return "Downloading model: $($Value.filename)`r`n$done / $('{0:N1} MB' -f ($Value.total_bytes / 1MB)) ($percent%)"
    }
    return "Downloading model: $($Value.filename)`r`n$done"
}

function Get-TunnelArguments($Remote, [string]$KeyFile, [string]$KnownHosts, [int]$Port) {
    if ($Remote.host -notmatch '^[a-zA-Z0-9.:-]+$' -or
        $Remote.ssh_port -lt 1 -or $Remote.ssh_port -gt 65535 -or
        $Remote.comfy_port -lt 1 -or $Remote.comfy_port -gt 65535 -or
        $Remote.host_key -notmatch '^ssh-ed25519 [A-Za-z0-9+/=]+$') {
        throw 'The starter returned an invalid connection address.'
    }
    [IO.File]::WriteAllText($KnownHosts, "hosted-comfyui $($Remote.host_key)`n", (New-Object Text.UTF8Encoding($false)))
    return @('-N', '-T', '-i', ('"{0}"' -f $KeyFile), '-p', $Remote.ssh_port,
        '-L', "127.0.0.1:${Port}:127.0.0.1:$($Remote.comfy_port)",
        '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes', '-o', 'ExitOnForwardFailure=yes',
        '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
        '-o', 'StrictHostKeyChecking=yes', '-o', 'HostKeyAlias=hosted-comfyui',
        '-o', ('"UserKnownHostsFile={0}"' -f $KnownHosts), "root@$($Remote.host)")
}

function Invoke-StarterRequest([string]$Uri, [string]$Method, $Headers, [string]$Body = '') {
    try {
        $arguments = @{Uri = $Uri; Method = $Method; Headers = $Headers; TimeoutSec = 30}
        if ($Body) { $arguments.ContentType = 'application/json'; $arguments.Body = $Body }
        return Invoke-RestMethod @arguments
    } catch {
        if ($_.Exception.Response -and [int]$_.Exception.Response.StatusCode -in @(401, 403)) {
            throw [UnauthorizedAccessException]::new('This invitation has expired or was revoked. Ask the owner for a new code.')
        }
        throw
    }
}

function Invoke-Starter($Access, [DateTime]$Deadline) {
    $uri = "https://api.runpod.ai/v2/$($Access.endpoint)"
    $headers = @{Authorization = "Bearer $($Access.key)"; 'User-Agent' = 'ComfyUI-Notch-Runpod/0.1'}
    $job = Invoke-StarterRequest "$uri/run" 'Post' $headers '{"input":{"action":"connect"}}'
    try {
        while ($job.status -in @('IN_QUEUE', 'IN_PROGRESS')) {
            if ((Get-Date) -gt $Deadline) { throw 'The starter did not respond in time. Try again later.' }
            Wait-Connection 3
            $job = Invoke-StarterRequest "$uri/status/$($job.id)" 'Get' $headers
        }
        if ($job.status -ne 'COMPLETED' -or $job.output.error) {
            throw 'The hosted server could not be started. Try again, or contact the person who invited you.'
        }
        return $job.output
    } finally {
        if ($job.status -in @('IN_QUEUE', 'IN_PROGRESS')) {
            try { Invoke-StarterRequest "$uri/cancel/$($job.id)" 'Post' $headers | Out-Null } catch { }
        }
    }
}

try {
    Write-State '1/5 Checking Windows SSH client'
    $sshCommand = Get-Command ssh.exe -ErrorAction SilentlyContinue
    if (!$sshCommand) { throw 'Windows OpenSSH Client is missing. Install it under Settings > Optional features, then reconnect.' }
    $ssh = $sshCommand.Source
    if ($LocalPort -lt 1024 -or $LocalPort -gt 65535) { throw 'Choose a local port between 1024 and 65535.' }
    if ($StartupTimeoutMinutes -lt 1 -or $StartupTimeoutMinutes -gt 60) { throw 'Choose a startup timeout between 1 and 60 minutes.' }
    if ([Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners().Port -contains $LocalPort) {
        throw "Local port $LocalPort is already in use. Close the existing launcher or choose another LocalPort."
    }
    $store = Join-Path $env:LOCALAPPDATA 'ComfyUI-Notch\Hosted'
    New-Item -ItemType Directory -Force -Path $store | Out-Null
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    $saved = [IO.Path]::GetFullPath($EnvFile)
    $lines = @()
    $encoded = ''
    if (Test-Path -LiteralPath $saved) {
        $lines = @(Get-Content -LiteralPath $saved)
        foreach ($line in $lines) {
            if ($line -match '^\s*HOSTED_COMFYUI_ACCESS\s*[:=]\s*(.+)') { $encoded = $Matches[1].Trim().Trim('"').Trim("'") }
        }
    }
    Write-State '2/5 Loading your tester invitation'
    if ($AccessFile) {
        $encoded = (Get-Content -LiteralPath $AccessFile -Raw).Trim()
    } elseif (!$encoded) {
        $secure = Read-Host 'Paste your invitation code' -AsSecureString
        $credential = New-Object System.Management.Automation.PSCredential('hosted-comfyui', $secure)
        $encoded = $credential.GetNetworkCredential().Password.Trim()
    }
    $encoded = $encoded.Replace('-', '+').Replace('_', '/')
    $encoded = $encoded.PadRight([int]([Math]::Ceiling($encoded.Length / 4.0) * 4), '=')
    $access = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($encoded)) | ConvertFrom-Json
    if ($access.version -ne 1 -or $access.endpoint -notmatch '^[a-z0-9]{8,40}$' -or
        $access.key -notmatch '^[A-Za-z0-9_-]{20,200}$' -or
        $access.ssh_key -notmatch '^-----BEGIN OPENSSH PRIVATE KEY-----') {
        throw 'This invitation code is invalid. Ask the owner for a new code.'
    }
    if (!(Test-Path -LiteralPath $saved)) { New-Item -ItemType File -Path $saved | Out-Null }
    $permissions = New-Object Security.AccessControl.FileSecurity
    $permissions.SetAccessRuleProtection($true, $false)
    $permissions.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule($identity, 'FullControl', 'Allow')))
    [IO.File]::SetAccessControl($saved, $permissions)
    $lines = @($lines | Where-Object { $_ -notmatch '^\s*HOSTED_COMFYUI_ACCESS\s*[:=]' })
    $lines += "HOSTED_COMFYUI_ACCESS=$encoded"
    [IO.File]::WriteAllLines($saved, $lines, (New-Object Text.UTF8Encoding($false)))
    $encoded = $null
    $work = if ($SessionDirectory) { [IO.Path]::GetFullPath($SessionDirectory) } else { Join-Path $store ([Guid]::NewGuid().ToString('N')) }
    New-Item -ItemType Directory -Path $work | Out-Null
    $permissions = New-Object Security.AccessControl.DirectorySecurity
    $permissions.SetAccessRuleProtection($true, $false)
    $permissions.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
        $identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
    [IO.Directory]::SetAccessControl($work, $permissions)
    $keyFile = Join-Path $work 'identity'
    [IO.File]::WriteAllText($keyFile, $access.ssh_key.Trim() + "`n", (New-Object Text.UTF8Encoding($false)))

    Write-State '3/5 Waking hosted ComfyUI (this may take a few minutes)'
    $deadline = (Get-Date).AddMinutes($StartupTimeoutMinutes)
    do {
        try { $remote = Invoke-Starter $access $deadline } catch [OperationCanceledException] {
            throw
        } catch [UnauthorizedAccessException] {
            throw
        } catch {
            if ((Get-Date) -gt $deadline) { throw }
            Write-State 'The starter is temporarily unavailable; retrying...'
            Wait-Connection 10
            continue
        }
        if ($remote.state -eq 'ready') { break }
        if ($remote.state -eq 'unavailable') { throw $remote.message }
        Write-State $remote.message
        if ((Get-Date) -gt $deadline) { throw 'Timed out waiting for the Pod. Your invitation is saved; try again later.' }
        Wait-Connection 10
    } while ($true)
    Write-State '4/5 Opening the local connection'
    $knownHosts = Join-Path $work 'known_hosts'
    $sshArgs = Get-TunnelArguments $remote $keyFile $knownHosts $LocalPort
    $url = "http://127.0.0.1:$LocalPort"
    $lastState = ''
    do {
        if ((Get-Date) -gt $deadline) { throw "ComfyUI did not become ready. Check $(Join-Path $work 'ssh.log')." }
        if ($tunnel -and $tunnel.HasExited) {
            if ((Get-Content -LiteralPath (Join-Path $work 'ssh.log') -Raw) -match 'Permission denied') {
                throw 'The server rejected your tunnel key. Ask the owner to renew your invitation.'
            }
            $remote = Invoke-Starter $access $deadline
            if ($remote.state -eq 'unavailable') { throw $remote.message }
            if ($remote.state -ne 'ready') { Wait-Connection 5; continue }
            $sshArgs = Get-TunnelArguments $remote $keyFile $knownHosts $LocalPort
        }
        if (!$tunnel -or $tunnel.HasExited) {
            $tunnel = Start-Process -FilePath $ssh -ArgumentList $sshArgs -PassThru -WindowStyle Hidden -RedirectStandardError (Join-Path $work 'ssh.log')
        }
        Wait-Connection 2
        if (!$tunnel.HasExited) {
            try {
                $stats = Invoke-RestMethod -Uri "$url/system_stats" -TimeoutSec 3
                $features = Invoke-RestMethod -Uri "$url/features" -TimeoutSec 3
                if ($stats.system.comfyui_version -and $features.extension.notch.output_transports -contains 'http') { break }
            } catch { }
        }
        if ($lastState -ne 'waiting') {
            Write-State '5/5 Waiting for ComfyUI and the plugin to finish loading'
            $lastState = 'waiting'
        }
    } while ($true)
    Write-Host ''
    Write-State 'Connected'
    Write-Host "WebUI: $url" -ForegroundColor Green
    Write-Host "Notch: Host 127.0.0.1 | Port $LocalPort | Transport HTTP"
    Write-Host 'Keep this window open while using hosted ComfyUI.'
    if (!$CheckOnly) { $storageProgress = New-StorageProgress $url }
    if ($ConnectorMode -and !$CheckOnly) {
        Write-Output "HOSTED_COMFYUI_READY=$url"
        while (!$tunnel.HasExited) {
            $progress = Get-StorageProgress $storageProgress
            if ($progress) {
                $encodedProgress = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($progress | ConvertTo-Json -Compress)))
                Write-Output "HOSTED_COMFYUI_STORAGE=$encodedProgress"
            }
            Wait-Connection 1
        }
        throw 'The connection ended or the Pod parked. Reconnect to start it again.'
    } elseif (!$CheckOnly) {
        Add-Type -AssemblyName System.Windows.Forms
        Add-Type -AssemblyName System.Drawing
        $form = New-Object Windows.Forms.Form
        $form.Text = 'Hosted ComfyUI - Connected'
        $form.ClientSize = New-Object Drawing.Size(560, 210)
        $form.StartPosition = 'CenterScreen'
        $label = New-Object Windows.Forms.Label
        $label.Text = "Connected. Keep this window open.`r`nNotch: 127.0.0.1 : $LocalPort   |   Transport: HTTP"
        $label.Location = New-Object Drawing.Point(20, 20)
        $label.Size = New-Object Drawing.Size(520, 45)
        $link = New-Object Windows.Forms.LinkLabel
        $link.Text = "Open ComfyUI: $url"
        $link.Location = New-Object Drawing.Point(20, 75)
        $link.Size = New-Object Drawing.Size(520, 30)
        $link.add_LinkClicked({ Start-Process $url })
        $modelStatus = New-Object Windows.Forms.Label
        $modelStatus.Location = New-Object Drawing.Point(20, 115)
        $modelStatus.Size = New-Object Drawing.Size(520, 70)
        $form.Controls.AddRange(@($label, $link, $modelStatus))
        $timer = New-Object Windows.Forms.Timer
        $timer.Interval = 1000
        $timer.add_Tick({
            if ($tunnel.HasExited) {
                $form.Text = 'Hosted ComfyUI - Disconnected'
                $label.Text = 'The connection ended or the Pod parked. Close this window and run the launcher again.'
                $link.Enabled = $false
                $timer.Stop()
            } else {
                $progress = Get-StorageProgress $storageProgress
                if ($progress) {
                    $modelStatus.Text = Format-StorageProgress $progress
                    if ($modelStatus.Text) { Write-State ($modelStatus.Text -replace '\r?\n', ' | ') }
                }
            }
        })
        $timer.Start()
        $form.ShowDialog() | Out-Null
        $timer.Stop()
        $form.Dispose()
    }
} catch [OperationCanceledException] {
    Write-State 'Connection closed'
} catch {
    Write-Host "Connection failed: $($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    if ($storageProgress) { $storageProgress.Client.Dispose() }
    if ($tunnel -and !$tunnel.HasExited) { $tunnel.Kill(); $tunnel.WaitForExit() }
    if ($work) {
        $keyPath = Join-Path $work 'identity'
        if (Test-Path -LiteralPath $keyPath) { Remove-Item -LiteralPath $keyPath -Force }
    }
}

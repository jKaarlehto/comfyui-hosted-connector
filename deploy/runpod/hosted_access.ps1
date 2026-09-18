function Test-WorkspaceGateway([string]$Value) {
    return $Value -cmatch '\Ahttps://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.workers\.dev\z'
}

function Get-WorkspaceHash([string]$Value) {
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hash.ComputeHash([Text.Encoding]::UTF8.GetBytes($Value)))).Replace('-', '').ToLowerInvariant() }
    finally { $hash.Dispose() }
}

function Protect-WorkspaceDirectory([string]$Path) {
    [IO.Directory]::CreateDirectory($Path) | Out-Null
    if (([IO.File]::GetAttributes($Path) -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'The workspace folder must not be a link.' }
    $permissions = New-Object Security.AccessControl.DirectorySecurity
    $permissions.SetAccessRuleProtection($true, $false)
    $permissions.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
        [Security.Principal.WindowsIdentity]::GetCurrent().User, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
    [IO.Directory]::SetAccessControl($Path, $permissions)
}

function Write-WorkspaceFile([string]$Path, [byte[]]$Bytes) {
    $temporary = $Path + '.' + [Guid]::NewGuid().ToString('N')
    try {
        [IO.File]::WriteAllBytes($temporary, $Bytes)
        if ([IO.File]::Exists($Path)) {
            if (([IO.File]::GetAttributes($Path) -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw 'The workspace file must not be a link.' }
            [IO.File]::Replace($temporary, $Path, [NullString]::Value)
        } else { [IO.File]::Move($temporary, $Path) }
    } finally { if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) } }
}

function Write-WorkspaceVault([string]$Path, $Value) {
    Add-Type -AssemblyName System.Security
    $bytes = [Text.Encoding]::UTF8.GetBytes(($Value | ConvertTo-Json -Compress))
    try {
        $encrypted = [Security.Cryptography.ProtectedData]::Protect($bytes, $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
        Write-WorkspaceFile $Path $encrypted
    } finally { [Array]::Clear($bytes, 0, $bytes.Length) }
}

function Read-WorkspaceVault([string]$Path) {
    Add-Type -AssemblyName System.Security
    $bytes = $null
    try {
        if ((Get-Item -LiteralPath $Path).Length -gt 32768 -or
            (([IO.File]::GetAttributes($Path) -band [IO.FileAttributes]::ReparsePoint) -ne 0)) { throw 'Invalid workspace file.' }
        $bytes = [Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($Path), $null, [Security.Cryptography.DataProtectionScope]::CurrentUser)
        return [Text.Encoding]::UTF8.GetString($bytes) | ConvertFrom-Json
    } catch { throw 'Could not open the saved workspace credentials for this Windows account.' }
    finally { if ($bytes) { [Array]::Clear($bytes, 0, $bytes.Length) } }
}

function New-WorkspaceIdentity([string]$Folder) {
    $keygen = Get-Command ssh-keygen.exe -ErrorAction SilentlyContinue
    if (!$keygen) { throw 'Windows OpenSSH Client is missing. Install it under Settings > Optional features, then reconnect.' }
    $temporary = Join-Path $Folder ('.keygen-' + [Guid]::NewGuid().ToString('N'))
    Protect-WorkspaceDirectory $temporary
    $path = Join-Path $temporary 'identity'
    try {
        $arguments = '-q -t ed25519 -N "" -C "ComfyUI-Notch" -f "' + $path + '"'
        $process = Start-Process -FilePath $keygen.Source -ArgumentList $arguments -PassThru -WindowStyle Hidden
        try {
            if (!$process.WaitForExit(10000)) { $process.Kill(); $process.WaitForExit(); throw 'Creating the workspace key timed out.' }
            if ($process.ExitCode -ne 0) { throw 'Could not create the workspace key.' }
        } finally { $process.Dispose() }
        $public = ([IO.File]::ReadAllText($path + '.pub').Trim() -split '\s+')[0..1] -join ' '
        $random = New-Object byte[] 32
        $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $generator.GetBytes($random) } finally { $generator.Dispose() }
        return [pscustomobject]@{ssh_key = [IO.File]::ReadAllText($path); public_key = $public;
            credential = ([BitConverter]::ToString($random)).Replace('-', '').ToLowerInvariant()}
    } finally {
        foreach ($file in @($path, ($path + '.pub'))) { if ([IO.File]::Exists($file)) { [IO.File]::Delete($file) } }
        [IO.Directory]::Delete($temporary, $false)
    }
}

function Invoke-WorkspaceRequest([string]$Gateway, [string]$Path, [string]$Method, $Headers, [string]$Body = '') {
    if (!(Test-WorkspaceGateway $Gateway) -or $Path -notmatch '\A/v1/(?:enroll|connect|jobs/[A-Za-z0-9_-]{1,128}(?:/cancel)?)\z') {
        throw 'The workspace address is invalid.'
    }
    Add-Type -AssemblyName System.Net.Http
    $handler = New-Object Net.Http.HttpClientHandler
    $handler.AllowAutoRedirect = $false
    $client = New-Object Net.Http.HttpClient($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(30)
    $client.MaxResponseContentBufferSize = 65536
    $request = New-Object Net.Http.HttpRequestMessage((New-Object Net.Http.HttpMethod($Method)), ($Gateway + $Path))
    $response = $null
    try {
        foreach ($name in $Headers.Keys) { $request.Headers.Add($name, [string]$Headers[$name]) }
        if ($Body) { $request.Content = New-Object Net.Http.StringContent($Body, [Text.Encoding]::UTF8, 'application/json') }
        try { $response = $client.SendAsync($request).GetAwaiter().GetResult() }
        catch { throw 'The workspace service is unavailable. Try again shortly.' }
        switch ([int]$response.StatusCode) {
            401 { throw [UnauthorizedAccessException]::new('Workspace access has been revoked or is invalid. Ask the owner for a new invitation.') }
            403 { throw [UnauthorizedAccessException]::new('Workspace access has been revoked or is invalid. Ask the owner for a new invitation.') }
            409 {
                if ($Path -eq '/v1/enroll') { throw [UnauthorizedAccessException]::new('This invitation has already been used on another device. Ask the owner for a new invitation.') }
                throw 'The workspace is already starting. Try again shortly.'
            }
            410 { throw [UnauthorizedAccessException]::new('This invitation has expired. Ask the owner for a new invitation.') }
        }
        if (!$response.IsSuccessStatusCode) { throw 'The workspace service is unavailable. Try again shortly.' }
        try { return $response.Content.ReadAsStringAsync().GetAwaiter().GetResult() | ConvertFrom-Json }
        catch { throw 'The workspace service returned an invalid response.' }
    } finally {
        if ($response) { $response.Dispose() }
        $request.Dispose(); $client.Dispose()
    }
}

function Open-WorkspaceAccess($Invitation, [string]$Root) {
    if (($Invitation.PSObject.Properties.Name | Sort-Object) -join ',' -cne 'gateway,invite_id,token,version' -or
        $Invitation.version -isnot [int] -or $Invitation.version -ne 2 -or
        $Invitation.gateway -isnot [string] -or !(Test-WorkspaceGateway $Invitation.gateway) -or
        $Invitation.invite_id -isnot [string] -or $Invitation.invite_id -cnotmatch '\A[0-9a-f]{32}\z' -or
        $Invitation.token -isnot [string] -or $Invitation.token -cnotmatch '\A(?:[0-9a-f]{64})?\z') {
        throw 'This invitation is invalid. Ask the owner for a new invitation.'
    }
    $folder = Join-Path $Root 'workspaces'
    Protect-WorkspaceDirectory $folder
    $id = Get-WorkspaceHash ($Invitation.gateway + '|' + $Invitation.invite_id)
    $path = Join-Path $folder ($id + '.dat')
    $lock = $null
    for ($attempt = 0; $attempt -lt 20 -and !$lock; $attempt++) {
        try { $lock = New-Object IO.FileStream(($path + '.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None) }
        catch [IO.IOException] { Start-Sleep -Milliseconds 250 }
    }
    if (!$lock) { throw 'This workspace is already being opened. Try again shortly.' }
    try {
        if ([IO.File]::Exists($path)) {
            $value = Read-WorkspaceVault $path
            if ($value.version -ne 2 -or $value.gateway -cne $Invitation.gateway -or $value.invite_id -cne $Invitation.invite_id -or
                $value.credential -cnotmatch '\A[0-9a-f]{64}\z' -or $value.public_key -cnotmatch '\Assh-ed25519 [A-Za-z0-9+/=]{40,100}\z' -or
                $value.ssh_key -notmatch '\A-----BEGIN OPENSSH PRIVATE KEY-----' -or
                $value.device_id -cnotmatch '\A(?:[0-9a-f]{32})?\z') { throw 'The saved workspace credentials are invalid.' }
        } else {
            if (!$Invitation.token) { throw 'This workspace is not saved on this computer. Open a new invitation from the owner.' }
            $identity = New-WorkspaceIdentity $folder
            $value = [pscustomobject]@{version = 2; gateway = $Invitation.gateway; invite_id = $Invitation.invite_id;
                device_id = ''; credential = $identity.credential; public_key = $identity.public_key; ssh_key = $identity.ssh_key;
                name = 'ComfyUI Notch'}
            Write-WorkspaceVault $path $value
        }
        if (!$value.device_id) {
            if (!$Invitation.token) { throw 'This invitation has not finished enrolling. Open the original invitation to try again.' }
            Write-State 'Accepting invitation'
            $deviceName = ($env:COMPUTERNAME -replace '[\x00-\x1f\x7f]', '').Trim()
            if (!$deviceName) { $deviceName = 'Windows connector' }
            if ($deviceName.Length -gt 80) { $deviceName = $deviceName.Substring(0, 80) }
            $body = @{invite_id = $Invitation.invite_id; token = $Invitation.token; public_key = $value.public_key;
                credential_hash = (Get-WorkspaceHash $value.credential); device_name = $deviceName} | ConvertTo-Json -Compress
            $reply = Invoke-WorkspaceRequest $value.gateway '/v1/enroll' 'POST' @{} $body
            if ($reply.device_id -isnot [string] -or $reply.device_id -cnotmatch '\A[0-9a-f]{32}\z' -or $reply.workspace_name -isnot [string] -or
                !$reply.workspace_name.Trim() -or $reply.workspace_name.Length -gt 80 -or $reply.workspace_name -match '[\x00-\x1f\x7f]') {
                throw 'The workspace service returned an invalid enrollment response.'
            }
            $value.device_id = $reply.device_id
            $value.name = $reply.workspace_name.Trim()
            Write-WorkspaceVault $path $value
            Write-State 'Saving workspace'
        } else { Write-State 'Opening saved workspace' }
        $descriptor = @{version = 2; gateway = $value.gateway; invite_id = $value.invite_id; name = $value.name; device_id = $value.device_id}
        Write-WorkspaceFile (Join-Path $folder ($id + '.json')) ([Text.Encoding]::UTF8.GetBytes(($descriptor | ConvertTo-Json -Compress)))
        return $value
    } finally { $lock.Dispose() }
}

function Get-WorkspaceBookmark($Access) {
    $value = @{version = 2; gateway = $Access.gateway; invite_id = $Access.invite_id; token = ''}
    return [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($value | ConvertTo-Json -Compress)))
}

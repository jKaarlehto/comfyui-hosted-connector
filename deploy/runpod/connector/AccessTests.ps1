$ErrorActionPreference = 'Stop'
$module = Join-Path (Split-Path $PSScriptRoot -Parent) 'hosted_access.ps1'
$tokens = $null; $errors = $null
[Management.Automation.Language.Parser]::ParseFile($module, [ref]$tokens, [ref]$errors) | Out-Null
if ($errors.Count) { throw ($errors | Out-String) }
. $module
$checks = 0
function Assert-Access([bool]$Value, [string]$Message) {
    $script:checks++
    if (!$Value) { throw $Message }
}
function Reject-Access([scriptblock]$Action, [string]$Message) {
    $rejected = $false
    try { & $Action | Out-Null } catch { $rejected = $true }
    Assert-Access $rejected $Message
}
function Write-State([string]$Message) { }
$gateway = 'https://fixture.example.workers.dev'
Assert-Access (Test-WorkspaceGateway $gateway) 'canonical gateway'
foreach ($bad in @('http://fixture.example.workers.dev', 'https://fixture.example.workers.dev/',
    'https://fixture.example.workers.dev:443', 'https://fixture.example.workers.dev@evil.test',
    'https://fixture.example.workers.dev.evil.test', 'https://127.0.0.1', 'https://fixture.example.workers.dev#token',
    'https://Fixture.example.workers.dev', "https://fixture.example.workers.dev`n")) {
    Assert-Access (!(Test-WorkspaceGateway $bad)) 'reject unsafe gateway'
}
Reject-Access { Invoke-WorkspaceRequest 'https://evil.test' '/v1/enroll' 'POST' @{} } 'restrict credential destination'
Reject-Access { Invoke-WorkspaceRequest $gateway '/v1/jobs/../../admin' 'GET' @{} } 'reject traversal before networking'
$testRoot = Join-Path ([IO.Path]::GetTempPath()) ('notch-workspace-test-' + [Guid]::NewGuid().ToString('N'))
$testRoot = [IO.Path]::GetFullPath($testRoot)
$script:requests = 0
$script:claimed = $null
$script:loseReply = $true
$invitation = [pscustomobject]@{version = 2; gateway = $gateway; invite_id = ('1' * 32); token = ('a' * 64)}
$hash = Get-WorkspaceHash ($gateway + '|' + $invitation.invite_id)
$vaultPath = Join-Path $testRoot ('workspaces/' + $hash + '.dat')
function Invoke-WorkspaceRequest([string]$Gateway, [string]$Path, [string]$Method, $Headers, [string]$Body = '') {
    $script:requests++
    Assert-Access ($Gateway -ceq $gateway -and $Path -eq '/v1/enroll' -and $Method -eq 'POST') 'enrollment endpoint'
    $request = $Body | ConvertFrom-Json
    $saved = Read-WorkspaceVault $vaultPath
    Assert-Access ($request.credential_hash -ceq (Get-WorkspaceHash $saved.credential)) 'credential persisted before redemption'
    Assert-Access ($request.public_key -ceq $saved.public_key) 'SSH identity persisted before redemption'
    Assert-Access (!$request.ssh_key -and !$request.credential) 'enrollment sends no private key or reconnect secret'
    if ($script:claimed) {
        Assert-Access ($request.credential_hash -ceq $script:claimed.credential_hash -and $request.public_key -ceq $script:claimed.public_key) 'retry uses original device binding'
    } else { $script:claimed = $request }
    if ($script:loseReply) { throw 'Simulated lost enrollment response' }
    return [pscustomobject]@{device_id = ('2' * 32); workspace_name = 'ComfyUI Notch Test'}
}
try {
    $invalid = [pscustomobject]@{version = 2; gateway = $gateway; invite_id = @('1' * 32); token = ('a' * 64)}
    Reject-Access { Open-WorkspaceAccess $invalid $testRoot } 'identifier arrays are rejected before enrollment'
    $invalid.invite_id = '1' * 32
    $invalid.gateway = @($gateway)
    Reject-Access { Open-WorkspaceAccess $invalid $testRoot } 'gateway arrays are rejected before enrollment'
    Assert-Access ($script:requests -eq 0) 'invalid invitation never reaches gateway'
    Reject-Access { Open-WorkspaceAccess $invitation $testRoot } 'lost response is recoverable'
    Assert-Access ([IO.File]::Exists($vaultPath)) 'pending identity retained'
    Assert-Access (![IO.File]::Exists([IO.Path]::ChangeExtension($vaultPath, '.json'))) 'pending identity not presented as enrolled'
    $script:loseReply = $false
    $access = Open-WorkspaceAccess $invitation $testRoot
    Assert-Access ($access.device_id -ceq ('2' * 32)) 'device enrolled'
    Assert-Access ($script:requests -eq 2) 'single retry'
    $descriptorPath = [IO.Path]::ChangeExtension($vaultPath, '.json')
    $descriptor = [IO.File]::ReadAllText($descriptorPath) | ConvertFrom-Json
    Assert-Access ($descriptor.name -eq 'ComfyUI Notch Test') 'saved workspace name'
    Assert-Access (!$descriptor.credential -and !$descriptor.ssh_key -and !$descriptor.token) 'descriptor has no credentials'
    $vaultBytes = [IO.File]::ReadAllBytes($vaultPath)
    Assert-Access (![Text.Encoding]::UTF8.GetString($vaultBytes).Contains($access.credential)) 'vault encrypted'
    Assert-Access (![Text.Encoding]::UTF8.GetString($vaultBytes).Contains('OPENSSH PRIVATE KEY')) 'private key encrypted'
    $bookmark = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String((Get-WorkspaceBookmark $access))) | ConvertFrom-Json
    Assert-Access ($bookmark.token -ceq '' -and $bookmark.invite_id -ceq $invitation.invite_id) 'bookmark contains no invitation token'
    $reopened = Open-WorkspaceAccess $bookmark $testRoot
    Assert-Access ($reopened.credential -ceq $access.credential -and $script:requests -eq 2) 'bookmark reopens locally without redemption'
    Reject-Access { Open-WorkspaceAccess $bookmark (Join-Path $testRoot 'other-computer') } 'bookmark cannot enroll another computer'
    Assert-Access ($script:requests -eq 2) 'missing bookmark never reaches gateway'
    $files = @(Get-ChildItem -LiteralPath (Join-Path $testRoot 'workspaces') -Recurse -File)
    Assert-Access (@($files | Where-Object { $_.Extension -notin @('.json', '.dat', '.lock') }).Count -eq 0) 'key generation leaves no plaintext files'
    $permissions = [IO.Directory]::GetAccessControl((Join-Path $testRoot 'workspaces'))
    Assert-Access $permissions.AreAccessRulesProtected 'workspace permissions private'
    $locked = New-Object IO.FileStream(($vaultPath + '.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    try { Reject-Access { Open-WorkspaceAccess $invitation $testRoot } 'concurrent launcher cannot replace device identity' }
    finally { $locked.Dispose() }
    [IO.File]::WriteAllBytes($vaultPath, [byte[]]@(1, 2, 3))
    Reject-Access { Open-WorkspaceAccess $invitation $testRoot } 'corrupted identity is never silently replaced'
    Assert-Access ($script:requests -eq 2) 'corruption cannot trigger another enrollment'
    $legacy = [pscustomobject]@{version = 1; endpoint = 'fixture123456'; key = ('A' * 40);
        ssh_key = ("-----BEGIN OPENSSH PRIVATE KEY-----`n" + ('A' * 80) + "`n-----END OPENSSH PRIVATE KEY-----`n")}
    Assert-Access (Test-LegacyInvitation $legacy) 'legacy invitation remains valid'
    $legacyAccess = Open-LegacyWorkspace $legacy $testRoot
    $legacyBookmark = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String((Get-WorkspaceBookmark $legacyAccess))) | ConvertFrom-Json
    Assert-Access ($legacyBookmark.version -eq 3 -and $legacyBookmark.workspace_id -cmatch '\A[0-9a-f]{64}\z') 'local-only legacy bookmark'
    Assert-Access (!$legacyBookmark.key -and !$legacyBookmark.ssh_key -and !$legacyBookmark.endpoint) 'legacy bookmark has no credentials'
    $legacyBase = Get-WorkspaceHash ('legacy|' + $legacyBookmark.workspace_id)
    $legacyPath = Join-Path $testRoot ('workspaces/' + $legacyBase + '.dat')
    $legacyDescriptor = [IO.File]::ReadAllText([IO.Path]::ChangeExtension($legacyPath, '.json')) | ConvertFrom-Json
    Assert-Access ($legacyDescriptor.version -eq 3 -and $legacyDescriptor.name.Contains($legacy.endpoint)) 'legacy saved-workspace descriptor'
    Assert-Access (!$legacyDescriptor.key -and !$legacyDescriptor.ssh_key -and !$legacyDescriptor.token) 'legacy descriptor public'
    Assert-Access (![Text.Encoding]::UTF8.GetString([IO.File]::ReadAllBytes($legacyPath)).Contains('OPENSSH PRIVATE KEY')) 'legacy vault encrypted'
    $legacyAgain = Open-LegacyWorkspace $legacyBookmark $testRoot
    Assert-Access ($legacyAgain.key -ceq $legacy.key -and $legacyAgain.ssh_key -ceq $legacy.ssh_key) 'legacy bookmark reconnects with preserved credentials'
    Assert-Access ($script:requests -eq 2) 'legacy import and reconnect are entirely local'
    Reject-Access { Open-LegacyWorkspace $legacyBookmark (Join-Path $testRoot 'missing-legacy') } 'legacy bookmark cannot grant access on another computer'
    $invalidLegacy = [pscustomobject]@{version = 3; workspace_id = @($legacyBookmark.workspace_id)}
    Reject-Access { Open-LegacyWorkspace $invalidLegacy $testRoot } 'legacy id arrays rejected'
    $invalidLegacy.workspace_id = '../../outside'
    Reject-Access { Open-LegacyWorkspace $invalidLegacy $testRoot } 'legacy traversal rejected'
    $legacy.key = ('B' * 40)
    Write-WorkspaceVault $legacyPath $legacy
    Reject-Access { Open-LegacyWorkspace $legacyBookmark $testRoot } 'legacy vault identity must match bookmark'
    Write-Host "Passed $checks workspace enrollment checks."
} finally {
    if ([IO.Directory]::Exists($testRoot)) {
        $expected = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\notch-workspace-test-'
        if (!$testRoot.StartsWith($expected, [StringComparison]::OrdinalIgnoreCase)) { throw 'Test cleanup path escaped its workspace.' }
        [IO.Directory]::Delete($testRoot, $true)
    }
}

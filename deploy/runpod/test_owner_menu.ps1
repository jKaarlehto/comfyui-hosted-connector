$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'manage_hosted_comfyui.ps1')

function Assert-True($Condition, [string]$Message) {
    if (!$Condition) { throw $Message }
}

Resolve-OwnerPython
$invokeHelper = ${function:Invoke-OwnerHelper}
$invokeAction = ${function:Invoke-OwnerAction}
$temporaryRoot = [IO.Path]::GetFullPath([IO.Path]::GetTempPath())
$temporary = Join-Path $temporaryRoot ('hosted-owner-test-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $temporary | Out-Null
$script:StateDir = Join-Path $temporary 'state with spaces'
$script:EnvFile = Join-Path $temporary 'owner settings.env'
$capture = Join-Path $temporary 'arguments.json'
$env:OWNER_MENU_CAPTURE = $capture
$pythonPrefix = $script:PythonArguments
try {
    $script:PythonArguments = $pythonPrefix + @('-c', "import json,os,sys;open(os.environ['OWNER_MENU_CAPTURE'],'w').write(json.dumps(sys.argv[1:]));sys.exit(0)")
    & $invokeHelper 'hosted.py' @('share', '--guest', 'alice')
    $received = Get-Content -LiteralPath $capture -Raw | ConvertFrom-Json
    Assert-True ($received[0] -eq (Join-Path $PSScriptRoot 'hosted.py')) 'Helper path changed.'
    Assert-True (($received[1..3] -join '|') -eq 'share|--guest|alice') 'Helper command changed.'
    Assert-True ($received[5] -eq $script:StateDir) 'State path with spaces was split.'
    Assert-True ($received[7] -eq $script:EnvFile) 'Environment path with spaces was split.'
    $location = (Get-Location).Path
    $script:PythonArguments = $pythonPrefix + @('-c', 'import sys;sys.exit(7)')
    $failed = $false
    try { & $invokeHelper 'runpod.py' @('status') } catch { $failed = $_.Exception.Message -match 'exit 7' }
    Assert-True $failed 'Native process failure was ignored.'
    Assert-True ((Get-Location).Path -eq $location) 'Working directory was not restored after failure.'

    $script:Calls = New-Object Collections.Generic.List[string]
    $script:Answers = New-Object Collections.Generic.Queue[string]
    function Invoke-OwnerHelper([string]$Helper, [string[]]$Arguments) { $script:Calls.Add($Helper + ':' + ($Arguments -join '|')) }
    function Require-Command { }
    function Connect-Owner { $script:Calls.Add('connect') }
    function Close-OwnerTunnel { $script:Calls.Add('close') }
    function Start-Process { $script:Calls.Add('open') }
    function Set-Clipboard([string]$Value) { $script:Clipboard = $Value }
    function Read-Host { return $script:Answers.Dequeue() }

    foreach ($choice in @('1', '3', '4', '5', '6')) { & $invokeAction $choice }
    Assert-True (($script:Calls -join ',') -eq 'runpod.py:setup,runpod.py:status,runpod.py:start,connect,runpod.py:stop,close') 'Basic menu routing failed.'
    $script:Calls.Clear()
    $script:Answers.Enqueue('n')
    & $invokeAction '2'
    Assert-True ($script:Calls.Count -eq 0) 'Declined deploy still ran.'
    $script:Answers.Enqueue('y')
    & $invokeAction '2'
    $script:Answers.Enqueue('y')
    & $invokeAction '7'
    Assert-True (($script:Calls -join ',') -eq 'runpod.py:deploy,hosted.py:setup') 'Setup/deploy routing failed.'
    $script:Calls.Clear()
    $script:Answers.Enqueue('alice')
    & $invokeAction '8'
    Assert-True (($script:Calls -join ',') -eq 'hosted.py:share|--guest|alice,open') 'Share must finish before opening its folder.'
    $script:Calls.Clear()
    $script:Answers.Enqueue('alice')
    $script:Answers.Enqueue('y')
    & $invokeAction '9'
    Assert-True (($script:Calls -join ',') -eq 'hosted.py:revoke|--guest|alice') 'Revoke routing failed.'
    $script:Answers.Enqueue('../escape')
    $failed = $false
    try { & $invokeAction '8' } catch { $failed = $true }
    Assert-True $failed 'Invalid guest name was accepted.'
    $script:Calls.Clear()
    $share = Join-Path $script:StateDir 'shares/alice'
    New-Item -ItemType Directory -Path $share -Force | Out-Null
    Set-Content -LiteralPath (Join-Path $share 'invitation-link.txt') -Value 'https://example.test/#fixture'
    $script:Answers.Enqueue('alice')
    $script:Answers.Enqueue('https://example.test/')
    & $invokeAction 'L'
    Assert-True (($script:Calls -join ',') -eq 'hosted.py:link|--guest|alice|--site-url|https://example.test/') 'Link routing failed.'
    Assert-True ($script:Clipboard -eq 'https://example.test/#fixture') 'Private link was not copied intact.'
    $script:Calls.Clear()
    & $invokeAction 'I'
    Assert-True (($script:Calls -join ',') -eq 'hosted.py:list') 'Invitation listing must use the offline helper command.'

    $script:Calls.Clear()
    $script:Answers.Enqueue('y')
    & $invokeAction 'A'
    & $invokeAction 'D'
    $script:Answers.Enqueue(('a' * 32))
    $script:Answers.Enqueue('y')
    & $invokeAction 'R'
    Assert-True (($script:Calls -join ',') -eq ('hosted.py:setup-gateway,hosted.py:devices,hosted.py:devices,hosted.py:revoke-device|--device|' + ('a' * 32))) 'Gateway menu routing failed.'

    $script:Calls.Clear()
    function Invoke-OwnerAction { throw 'simulated action failure' }
    $script:Answers.Enqueue('3')
    $script:Answers.Enqueue('')
    $script:Answers.Enqueue('0')
    Show-OwnerMenu
    Assert-True (($script:Calls -join ',') -eq 'close') 'Menu did not survive failure and close normally.'
    Write-Host 'Owner menu command, failure and navigation tests passed.' -ForegroundColor Green
} finally {
    Remove-Item Env:OWNER_MENU_CAPTURE -ErrorAction SilentlyContinue
    $resolved = [IO.Path]::GetFullPath($temporary)
    if ($resolved.StartsWith($temporaryRoot, [StringComparison]::OrdinalIgnoreCase) -and
        [IO.Path]::GetFileName($resolved).StartsWith('hosted-owner-test-')) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

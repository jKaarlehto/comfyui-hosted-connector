$ErrorActionPreference = 'Stop'
$scriptPath = Join-Path (Split-Path $PSScriptRoot -Parent) 'start_hosted_comfyui.ps1'
$tokens = $null; $errors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($scriptPath, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw ($errors | Out-String) }
$functions = $ast.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] }, $false)
foreach ($name in @('New-StorageProgress', 'Get-StorageProgress', 'Format-StorageProgress')) {
    $function = $functions | Where-Object Name -eq $name
    if (!$function) { throw "Missing function: $name" }
    Invoke-Expression $function.Extent.Text
}
Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
public sealed class ProgressFixture : IDisposable {
    readonly TcpListener listener = new TcpListener(IPAddress.Loopback, 0);
    readonly Thread thread;
    volatile bool stopped;
    public string Body = "{}";
    public int Code = 200, Delay;
    int requests;
    public int Requests { get { return Interlocked.CompareExchange(ref requests, 0, 0); } }
    public string Url { get { return "http://127.0.0.1:" + ((IPEndPoint)listener.LocalEndpoint).Port; } }
    public ProgressFixture() {
        listener.Start(); thread = new Thread(Run); thread.Start();
    }
    void Run() {
        while (!stopped) {
            try {
                if (!listener.Pending()) { Thread.Sleep(5); continue; }
                using (TcpClient client = listener.AcceptTcpClient()) {
                    client.ReceiveTimeout = 1000; client.SendTimeout = 1000;
                    using (NetworkStream stream = client.GetStream()) {
                        var reader = new StreamReader(stream, Encoding.ASCII, false, 1024, true);
                        string request = reader.ReadLine();
                        if (request != "GET /hosted_comfyui/storage HTTP/1.1") throw new Exception("Unexpected request");
                        while (!String.IsNullOrEmpty(reader.ReadLine())) {}
                        Interlocked.Increment(ref requests);
                        int delay = Delay; string body = Body; int code = Code;
                        Thread.Sleep(delay);
                        byte[] bytes = Encoding.UTF8.GetBytes(body);
                        byte[] head = Encoding.ASCII.GetBytes("HTTP/1.1 " + code + " Test\r\nContent-Type: application/json\r\nContent-Length: " + bytes.Length + "\r\nConnection: close\r\n\r\n");
                        stream.Write(head, 0, head.Length); stream.Write(bytes, 0, bytes.Length);
                    }
                }
            } catch (IOException) {} catch (SocketException) { if (stopped) return; }
        }
    }
    public void Dispose() { stopped = true; listener.Stop(); thread.Join(5000); }
}
'@
$script:checks = 0
function Assert-Progress([bool]$Condition, [string]$Message) {
    $script:checks++
    if (!$Condition) { throw "Progress test failed: $Message" }
}
function Read-Progress($Poller) {
    $Poller.NextPoll = [DateTime]::MinValue
    $null = Get-StorageProgress $Poller
    $deadline = (Get-Date).AddSeconds(5)
    while (!$Poller.Task.IsCompleted -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 10 }
    if (!$Poller.Task.IsCompleted) { throw 'Progress request timed out in test' }
    return Get-StorageProgress $Poller
}
$server = New-Object ProgressFixture
$poller = $null
try {
    $server.Body = '{"phase":"downloading","filename":"models/example.safetensors","completed_bytes":5242880,"total_bytes":10485760}'
    $poller = New-StorageProgress $server.Url
    $value = Read-Progress $poller
    Assert-Progress ($value.phase -eq 'downloading' -and $value.completed_bytes -eq 5242880) 'download bytes'
    Assert-Progress ((Format-StorageProgress $value) -match '50%') 'formatted progress'
    Assert-Progress ($null -eq (Read-Progress $poller)) 'unchanged sample suppressed'
    $server.Body = '{"phase":"verifying","filename":"models/example.safetensors","completed_bytes":10485760,"total_bytes":10485760}'
    $value = Read-Progress $poller
    Assert-Progress ($value.phase -eq 'verifying') 'verifying state'
    $server.Body = '{"phase":"idle","filename":"","completed_bytes":0,"total_bytes":0}'
    $value = Read-Progress $poller
    Assert-Progress ($value.phase -eq 'idle' -and (Format-StorageProgress $value) -eq '') 'idle clears progress'
    $server.Body = '{"phase":"error","filename":"models/example.safetensors","completed_bytes":0,"total_bytes":0,"error":"Try again"}'
    $value = Read-Progress $poller
    Assert-Progress ((Format-StorageProgress $value) -match 'Try again') 'transfer error retained without disconnect'
    $server.Body = '{"phase":"downloading","filename":"bad","completed_bytes":-1,"total_bytes":0}'
    Assert-Progress ($null -eq (Read-Progress $poller)) 'invalid byte count ignored'
    $server.Body = 'not json'
    Assert-Progress ($null -eq (Read-Progress $poller)) 'malformed body ignored'
    $server.Body = '{"phase":"downloading","filename":"models/example","completed_bytes":2097152,"total_bytes":0}'
    Assert-Progress ((Read-Progress $poller).completed_bytes -eq 2097152) 'unknown size progress'
    $server.Body = '{"phase":"downloading","filename":"models/example","completed_bytes":3145728,"total_bytes":0}'
    Assert-Progress ((Read-Progress $poller).completed_bytes -eq 3145728) 'unknown size progress advances'
    $server.Body = 'a' * 9000
    Assert-Progress ($null -eq (Read-Progress $poller)) 'oversized response ignored'
    $server.Body = '{"phase":"idle","filename":"","completed_bytes":0,"total_bytes":0}'
    $server.Delay = 1000
    $poller.NextPoll = [DateTime]::MinValue
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $null = Get-StorageProgress $poller
    Assert-Progress ($clock.ElapsedMilliseconds -lt 500 -and $null -ne $poller.Task) 'request is asynchronous'
    $clock.Restart(); $null = Get-StorageProgress $poller
    Assert-Progress ($clock.ElapsedMilliseconds -lt 500) 'in-flight poll does not block'
    while (!$poller.Task.IsCompleted) { Start-Sleep -Milliseconds 10 }
    $null = Get-StorageProgress $poller
    $server.Delay = 0; $server.Code = 404
    Assert-Progress ($null -eq (Read-Progress $poller) -and $poller.Unsupported) 'old pod quietly disables progress'
    $requests = $server.Requests
    $null = Get-StorageProgress $poller
    Assert-Progress ($server.Requests -eq $requests -and $null -eq $poller.Task) '404 is not repeatedly polled'
    Write-Host "Passed $script:checks asynchronous launcher progress checks."
} finally {
    if ($poller) { $poller.Client.Dispose() }
    $server.Dispose()
}

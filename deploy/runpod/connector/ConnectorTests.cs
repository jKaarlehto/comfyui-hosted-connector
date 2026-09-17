using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using HostedComfyUI;

internal static class ConnectorTests
{
    private static int checks;
    private static void Check(bool value) { checks++; if (!value) throw new Exception("Check " + checks + " failed"); }
    private static void Reject(Action action)
    {
        checks++;
        try { action(); } catch (ArgumentException) { return; }
        throw new Exception("Invalid invitation accepted at check " + checks);
    }
    private static string Encode(Dictionary<string, object> fields)
    {
        return Convert.ToBase64String(Encoding.UTF8.GetBytes(new JavaScriptSerializer().Serialize(fields)));
    }
    private static int Main()
    {
        try
        {
            var fields = new Dictionary<string, object> {
                {"version", 1}, {"endpoint", "abcdefgh12345"}, {"key", new String('A', 40)},
                {"ssh_key", "-----BEGIN OPENSSH PRIVATE KEY-----\n" + new String('A', 80) + "\n-----END OPENSSH PRIVATE KEY-----\n"}
            };
            string encoded = Encode(fields);
            Check(Invitation.Normalize(encoded) == encoded);
            string urlEncoded = encoded.TrimEnd('=').Replace('+', '-').Replace('/', '_');
            Check(Invitation.DecodeUri("hosted-comfyui://connect#" + urlEncoded) == encoded);
            Check(Invitation.DecodeUri("hosted-comfyui://connect/#" + encoded) == encoded);
            foreach (string prefix in new [] {"https://connect#", "hosted-comfyui://run#", "hosted-comfyui://user@connect#", "hosted-comfyui://connect:8188#", "hosted-comfyui://connect/run#", "hosted-comfyui://connect?a=b#"})
                Reject(delegate { Invitation.DecodeUri(prefix + encoded); });
            Reject(delegate { Invitation.DecodeUri("hosted-comfyui://connect"); });
            Reject(delegate { Invitation.DecodeUri("hosted-comfyui://connect#" + new String('A', 16000)); });
            Reject(delegate { Invitation.Normalize("'$([bad])"); });
            Reject(delegate { Invitation.Normalize("A"); });
            fields["endpoint"] = "../../wrong";
            Reject(delegate { Invitation.Normalize(Encode(fields)); });
            fields["endpoint"] = "abcdefgh12345";
            fields["key"] = "x\" -Command malicious";
            Reject(delegate { Invitation.Normalize(Encode(fields)); });
            fields["key"] = new String('A', 40);
            fields["version"] = "1";
            Reject(delegate { Invitation.Normalize(Encode(fields)); });
            fields["version"] = 2;
            Reject(delegate { Invitation.Normalize(Encode(fields)); });
            fields["version"] = 1;
            fields["command"] = "cmd.exe";
            Reject(delegate { Invitation.Normalize(Encode(fields)); });
            fields.Remove("command");
            fields["ssh_key"] = "-----BEGIN OPENSSH PRIVATE KEY-----\nevil";
            Reject(delegate { Invitation.Normalize(Encode(fields)); });
            Check(Storage.Quote(@"C:\User Data\test.ps1") == "\"C:\\User Data\\test.ps1\"");
            Reject(delegate { Storage.Quote("bad\"path"); });
            TestPresence();
            using (var rejectedInstall = Process.Start(new ProcessStartInfo(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "HostedComfyUIConnector.exe"), "--install unexpected")
                { UseShellExecute = false, CreateNoWindow = true, RedirectStandardError = true }))
            {
                Check(rejectedInstall.WaitForExit(5000));
                Check(rejectedInstall.ExitCode == 2);
                Check(rejectedInstall.StandardError.ReadToEnd().Contains("no additional arguments"));
            }
            using (var rejectedPresence = Process.Start(new ProcessStartInfo(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "HostedComfyUIConnector.exe"), "--presence unexpected")
                { UseShellExecute = false, CreateNoWindow = true }))
            {
                Check(rejectedPresence.WaitForExit(5000));
                Check(rejectedPresence.ExitCode == 2);
            }
            using (var job = new ChildJob())
            using (var process = Process.Start(new ProcessStartInfo(Program.PowerShell, "-NoProfile -Command Start-Sleep -Seconds 30") { UseShellExecute = false, CreateNoWindow = true }))
            {
                job.Add(process);
                job.Dispose();
                Check(process.WaitForExit(5000));
            }
            string pidFile = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "child-" + Guid.NewGuid().ToString("N"));
            string command = "$child = Start-Process -FilePath '" + Program.PowerShell.Replace("'", "''") +
                "' -ArgumentList '-NoProfile -Command Start-Sleep -Seconds 30' -PassThru -WindowStyle Hidden; [IO.File]::WriteAllText('" +
                pidFile.Replace("'", "''") + "', [string]$child.Id); Start-Sleep -Seconds 30";
            try
            {
                using (var job = new ChildJob())
                using (var parent = Process.Start(new ProcessStartInfo(Program.PowerShell, "-NoProfile -EncodedCommand " + Convert.ToBase64String(Encoding.Unicode.GetBytes(command))) { UseShellExecute = false, CreateNoWindow = true }))
                {
                    job.Add(parent);
                    for (int attempt = 0; attempt < 100 && !File.Exists(pidFile); attempt++) Thread.Sleep(100);
                    Check(File.Exists(pidFile));
                    using (var child = Process.GetProcessById(Int32.Parse(File.ReadAllText(pidFile))))
                    {
                        job.Dispose();
                        Check(parent.WaitForExit(5000));
                        Check(child.WaitForExit(5000));
                    }
                }
            }
            finally { if (File.Exists(pidFile)) File.Delete(pidFile); }
            Console.WriteLine("Passed " + checks + " connector checks.");
            return 0;
        }
        catch (Exception error) { Console.Error.WriteLine(error); return 1; }
    }

    private static void TestPresence()
    {
        const string origin = "https://jkaarlehto.github.io";
        const string nonce = "0123456789abcdef0123456789abcdef";
        string request = "GET /status?nonce=" + nonce + " HTTP/1.1\r\nHost: 127.0.0.1:18187\r\nOrigin: " + origin + "\r\n\r\n";
        Check(Presence.OriginFromSite(origin + "/comfyui-hosted-connector/") == origin);
        Check(Presence.OriginFromSite("") == null);
        Check(Presence.OriginFromSite("http://example.com") == null);
        Check(Presence.OriginFromSite("https://user@example.com/") == null);
        Check(Presence.OriginFromSite(origin + "?secret=1") == null);
        string executable = @"C:\User Data\HostedComfyUIConnector.exe";
        Check(Presence.RegistrationMatches(Storage.Quote(executable) + " \"%1\"", executable, true));
        Check(!Presence.RegistrationMatches(Storage.Quote(executable) + " \"%1\"", executable, false));
        Check(!Presence.RegistrationMatches("\"C:\\Other.exe\" \"%1\"", executable, true));
        Check(!Presence.RegistrationMatches(Storage.Quote(executable) + " --run \"%1\"", executable, true));
        string response = Presence.Response(request, origin, true);
        Check(response.StartsWith("HTTP/1.1 200 OK\r\n"));
        Check(response.Contains("Access-Control-Allow-Origin: " + origin + "\r\n"));
        Check(!response.Contains("Access-Control-Allow-Credentials"));
        Check(response.Contains("Cache-Control: no-store\r\n"));
        var fields = new JavaScriptSerializer().DeserializeObject(response.Substring(response.IndexOf("\r\n\r\n") + 4)) as Dictionary<string, object>;
        Check(fields.Count == 4 && (string)fields["app"] == "hosted-comfyui-connector" && (int)fields["protocol"] == 1 &&
            (string)fields["version"] == Presence.Version && (string)fields["nonce"] == nonce);
        Check(Presence.Response(request, origin, false).StartsWith("HTTP/1.1 404"));
        Check(Presence.Response(request, null, true).StartsWith("HTTP/1.1 403"));
        foreach (string invalid in new[] {
            request.Replace("127.0.0.1:18187", "evil.example:18187"),
            request.Replace("127.0.0.1:18187", "localhost:18187"),
            request.Replace("127.0.0.1:18187", "127.0.0.1:8188"),
            request.Replace(origin, "https://evil.example"),
            request.Replace(origin, "null"),
            request.Replace("Origin: " + origin + "\r\n", ""),
            request.Replace("GET ", "POST "),
            request.Replace("GET ", "CONNECT "),
            request.Replace("/status?", "/connect?"),
            request.Replace("/status?", "http://127.0.0.1:18187/status?"),
            request.Replace(nonce, "malicious\""),
            request.Replace(nonce, nonce + "&action=start"),
            request.Replace("\r\n\r\n", "\r\nhost: 127.0.0.1:18187\r\n\r\n"),
            request.Replace("\r\n\r\n", "\r\nTransfer-Encoding: chunked\r\n\r\n"),
            request.Replace("\r\n\r\n", "\r\nContent-Length: 1\r\n\r\n"),
            request.Replace("\r\n\r\n", "\r\nAuthorization: Bearer secret\r\n\r\n"),
            request.Replace("\r\n\r\n", "\r\nCookie: secret\r\n\r\n"),
            request.Replace("\r\n\r\n", "\r\nBad Header: value\r\n\r\n"),
            request + request,
            request.Substring(0, request.Length - 2),
            request.Replace("Origin:", "Origin :"),
            request.Replace("Origin:", " Origin:"),
            request.Replace("\r\n\r\n", "\r\nX-Long: " + new string('a', 8200) + "\r\n\r\n") })
        {
            string rejected = Presence.Response(invalid, origin, true);
            Check(rejected.StartsWith("HTTP/1.1 4") && !rejected.Contains("Access-Control-Allow-Origin") && !rejected.Contains("secret"));
        }
        string preflight = request.Replace("GET ", "OPTIONS ").Replace("\r\n\r\n", "\r\nAccess-Control-Request-Method: GET\r\nAccess-Control-Request-Private-Network: true\r\n\r\n");
        Check(Presence.Response(preflight, origin, true).StartsWith("HTTP/1.1 204"));
        Check(Presence.Response(preflight, origin, true).Contains("Access-Control-Allow-Private-Network: true"));
        Check(Presence.Response(preflight.Replace("Method: GET", "Method: POST"), origin, true).StartsWith("HTTP/1.1 403"));
        Check(Presence.Response(preflight.Replace("\r\n\r\n", "\r\nAccess-Control-Request-Headers: authorization\r\n\r\n"), origin, true).StartsWith("HTTP/1.1 403"));
        using (var stop = new ManualResetEvent(false))
        using (var ready = new ManualResetEvent(false))
        {
            var listener = new TcpListener(IPAddress.Loopback, 0);
            int installed = 1;
            Exception failure = null;
            var thread = new Thread(delegate()
            {
                try { Presence.Listen(listener, origin, stop, ready, delegate { return Interlocked.CompareExchange(ref installed, 0, 0) == 1; }); }
                catch (Exception error) { failure = error; }
            });
            thread.Start();
            try
            {
                Check(ready.WaitOne(3000));
                int port = ((IPEndPoint)listener.LocalEndpoint).Port;
                Check(Probe(port, request).StartsWith("HTTP/1.1 200"));
                Check(Probe(port, preflight).StartsWith("HTTP/1.1 204"));
                Check(Probe(port, request.Replace(origin, "https://evil.example")).StartsWith("HTTP/1.1 403"));
                using (var stalled = new TcpClient())
                {
                    stalled.Connect(IPAddress.Loopback, port);
                    stalled.GetStream().WriteByte((byte)'G');
                    stalled.GetStream().ReadTimeout = 4000;
                    var clock = Stopwatch.StartNew();
                    Check(stalled.GetStream().ReadByte() == -1);
                    Check(clock.ElapsedMilliseconds < 3500);
                }
                Interlocked.Exchange(ref installed, 0);
                Check(thread.Join(2500));
                Check(failure == null);
            }
            finally { stop.Set(); thread.Join(3000); }
        }
    }

    private static string Probe(int port, string request)
    {
        using (var client = new TcpClient())
        {
            client.Connect(IPAddress.Loopback, port);
            client.GetStream().ReadTimeout = 3000;
            byte[] bytes = Encoding.ASCII.GetBytes(request);
            client.GetStream().Write(bytes, 0, bytes.Length);
            using (var reader = new StreamReader(client.GetStream())) return reader.ReadToEnd();
        }
    }
}

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;
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
    [STAThread]
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
            string session;
            string token = new String('a', 32);
            Check(Invitation.DecodeUri("hosted-comfyui://connect?session=" + token + "#" + urlEncoded, out session) == encoded && session == token);
            Check(Invitation.DecodeUri("hosted-comfyui://connect#" + encoded, out session) == encoded && session == "");
            foreach (string query in new[] { "session=short", "session=" + token.ToUpperInvariant(), "session=" + token + "&session=" + token,
                "session=" + token + "&action=start", "nonce=" + token, "session=" + token + "%0a" })
                Reject(delegate { Invitation.DecodeUri("hosted-comfyui://connect?" + query + "#" + encoded); });
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
            TestModelProgress();
            TestLiveStatus();
            TestEnrollment();
            TestWorkspacesAndForm();
            TestLegacyMigration();
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

    private static void TestModelProgress()
    {
        var fields = new Dictionary<string, object> { { "phase", "downloading" }, { "filename", "models/checkpoints/example.safetensors" },
            { "completed_bytes", 5368709120L }, { "total_bytes", 10737418240L } };
        ModelProgress value;
        Check(ModelProgress.TryDecode(Encode(fields), out value) && value.Percent == 50 && value.Text.Contains("50%"));
        Check(value.Completed == 5368709120L && value.Total == 10737418240L && value.Text.Contains("MB"));
        fields["phase"] = "verifying";
        Check(ModelProgress.TryDecode(Encode(fields), out value) && value.Text.StartsWith("Verifying model:"));
        fields["phase"] = "idle";
        Check(ModelProgress.TryDecode(Encode(fields), out value) && value.Text == "");
        fields["phase"] = "error"; fields["error"] = "Retry\r\nrequest";
        Check(ModelProgress.TryDecode(Encode(fields), out value) && value.Error == "Retry  request" && value.Text.StartsWith("Model download failed:"));
        fields["phase"] = "downloading"; fields["total_bytes"] = 0;
        Check(ModelProgress.TryDecode(Encode(fields), out value) && value.Percent == -1 && !value.Text.Contains("%"));
        fields["total_bytes"] = 1;
        Check(!ModelProgress.TryDecode(Encode(fields), out value));
        fields["completed_bytes"] = -1;
        Check(!ModelProgress.TryDecode(Encode(fields), out value));
        fields["completed_bytes"] = "1";
        Check(!ModelProgress.TryDecode(Encode(fields), out value));
        fields["completed_bytes"] = 1; fields["phase"] = "execute";
        Check(!ModelProgress.TryDecode(Encode(fields), out value));
        fields["phase"] = "idle"; fields["filename"] = new String('a', 1025);
        Check(!ModelProgress.TryDecode(Encode(fields), out value));
        Check(!ModelProgress.TryDecode("invalid", out value));
        Check(!ModelProgress.TryDecode(new String('A', 12001), out value));
        Check(!ModelProgress.TryDecode(null, out value));
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
        Check(fields.Count == 6 && (int)fields["enrollment"] == 2 && (int)fields["live_status"] == 1 && (string)fields["app"] == "hosted-comfyui-connector" && (int)fields["protocol"] == 1 &&
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

    private static Dictionary<string, object> ReadJson(string text)
    {
        return (Dictionary<string, object>)new JavaScriptSerializer().DeserializeObject(text);
    }

    private static void TestEnrollment()
    {
        var value = new Dictionary<string, object> { { "version", 2 }, { "gateway", "https://workspace.example.workers.dev" },
            { "invite_id", new String('a', 32) }, { "token", new String('b', 64) } };
        string original = Invitation.Normalize(Encode(value));
        Check(Invitation.DecodeUri("hosted-comfyui://connect#" + original) == original);
        Reject(delegate { Invitation.SavedBookmark(original); });
        value["token"] = "";
        string bookmark = Invitation.SavedBookmark(Encode(value));
        Check(Invitation.DecodeUri("hosted-comfyui://connect#" + bookmark) == bookmark);
        Check(Invitation.Identity(original) == Invitation.Identity(bookmark));
        foreach (object invalid in new object[] { new String('B', 64), "short", 12, null, new String('a', 65) })
        {
            value["token"] = invalid;
            Reject(delegate { Invitation.Normalize(Encode(value)); });
        }
        value["token"] = new String('b', 64);
        foreach (object invalid in new object[] { "../test", new String('A', 32), new String('a', 31), 12, null })
        {
            value["invite_id"] = invalid;
            Reject(delegate { Invitation.Normalize(Encode(value)); });
        }
        value["invite_id"] = new String('a', 32);
        foreach (string invalid in new[] { "http://workspace.example.workers.dev", "https://localhost", "https://127.0.0.1", "https://[::1]",
            "https://10.0.0.1", "https://example.com", "https://workspace.example.workers.dev.evil.example", "https://user@workspace.example.workers.dev",
            "https://workspace.example.workers.dev:443", "https://workspace.example.workers.dev:8188", "https://workspace.example.workers.dev/",
            "https://workspace.example.workers.dev/path", "https://workspace.example.workers.dev?key=x", "https://workspace.example.workers.dev#key",
            "https://workspace.example.workers.dev\r\n", "https://Workspace.example.workers.dev", "https://a..workers.dev", "https://-a.example.workers.dev" })
        {
            value["gateway"] = invalid;
            Reject(delegate { Invitation.Normalize(Encode(value)); });
        }
        value["gateway"] = "https://workspace.example.workers.dev";
        value["extra"] = "command";
        Reject(delegate { Invitation.Normalize(Encode(value)); });
        value.Remove("extra"); value.Remove("token");
        Reject(delegate { Invitation.Normalize(Encode(value)); });
    }

    private static void TestWorkspacesAndForm()
    {
        string root = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "workspaces-" + Guid.NewGuid().ToString("N"));
        var descriptor = new Dictionary<string, object> { { "version", 2 }, { "gateway", "https://workspace.example.workers.dev" },
            { "invite_id", new String('a', 32) }, { "device_id", new String('c', 32) }, { "name", "My ComfyUI workspace" } };
        var serializer = new JavaScriptSerializer();
        try
        {
            Workspace item = Workspace.Parse(serializer.Serialize(descriptor));
            Check(item.Name == "My ComfyUI workspace" && item.Id.Length == 64 && Invitation.Identity(item.Bookmark) == item.Identity);
            Check(Invitation.SavedBookmark(item.Bookmark) == item.Bookmark && !Encoding.UTF8.GetString(Convert.FromBase64String(item.Bookmark)).Contains("device_id"));
            foreach (object invalid in new object[] { "", " leading space", "trailing space ", "bad\r\nname", new String('x', 81), 12, null })
            {
                descriptor["name"] = invalid;
                Reject(delegate { Workspace.Parse(serializer.Serialize(descriptor)); });
            }
            descriptor["name"] = "My ComfyUI workspace";
            descriptor["device_id"] = "../../escape";
            Reject(delegate { Workspace.Parse(serializer.Serialize(descriptor)); });
            descriptor["device_id"] = new String('c', 32);
            descriptor["token"] = "should-not-be-here";
            Reject(delegate { Workspace.Parse(serializer.Serialize(descriptor)); });
            descriptor.Remove("token");
            string folder = Path.Combine(root, "workspaces");
            Storage.ProtectDirectory(folder);
            File.WriteAllText(Path.Combine(folder, item.Id + ".json"), serializer.Serialize(descriptor));
            Check(Workspace.Load(root).Count == 0);
            File.WriteAllBytes(Path.Combine(folder, item.Id + ".dat"), new byte[] { 1 });
            Check(Workspace.Load(root).Count == 1);
            File.WriteAllText(Path.Combine(folder, "wrong-name.json"), serializer.Serialize(descriptor));
            File.WriteAllText(Path.Combine(folder, "broken.json"), "{");
            Check(Workspace.Load(root).Count == 1);
            Application.EnableVisualStyles();
            using (var form = new ConnectorForm(root))
            {
                form.StartPosition = FormStartPosition.Manual; form.Location = new Point(-32000, -32000); form.ShowInTaskbar = false; form.Opacity = 0;
                form.Show(); Application.DoEvents();
                var list = (ListBox)form.Controls.Find("savedWorkspaces", true)[0];
                var connect = (Button)form.Controls.Find("connect", true)[0];
                var close = (Button)form.Controls.Find("disconnect", true)[0];
                var open = (Button)form.Controls.Find("openComfyUI", true)[0];
                var details = (Button)form.Controls.Find("details", true)[0];
                var log = (TextBox)form.Controls.Find("connectionLog", true)[0];
                Check(form.Text.Contains("ComfyUI Notch") && list.Items.Count == 1 && connect.Enabled && !open.Enabled && !close.Enabled);
                Check(!log.Visible && details.Text == "Show details");
                typeof(ConnectorForm).GetField("activeInvitation", BindingFlags.NonPublic | BindingFlags.Instance).SetValue(form, item.Bookmark);
                form.SetPhase("starting", "Accepting invitation");
                Check(!connect.Enabled && close.Enabled && !open.Enabled && form.Controls.Find("summary", true)[0].Text == item.Name + Environment.NewLine + "Accepting invitation");
                form.SetPhase("connected", "Your workspace is ready. Keep this window open while connected.");
                Check(open.Enabled && close.Enabled && !connect.Enabled);
                descriptor["name"] = "Another workspace"; descriptor["invite_id"] = new String('b', 32);
                list.Items.Add(Workspace.Parse(serializer.Serialize(descriptor))); list.SelectedIndex = 1;
                Check(connect.Enabled && connect.Text == "Connect" && form.Controls.Find("summary", true)[0].Text.StartsWith(item.Name + Environment.NewLine));
                list.SelectedIndex = 0; list.Items.RemoveAt(1);
                Check(!connect.Enabled);
                using (var bitmap = new Bitmap(form.Width, form.Height))
                {
                    form.DrawToBitmap(bitmap, new Rectangle(Point.Empty, bitmap.Size));
                    bitmap.Save(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "connector-preview.png"));
                }
                int beforeModel = form.ClientSize.Height;
                ModelProgress download;
                Check(ModelProgress.TryDecode(Encode(new Dictionary<string, object> { { "phase", "downloading" }, { "filename", "models/checkpoints/example.safetensors" },
                    { "completed_bytes", 5368709120L }, { "total_bytes", 10737418240L } }), out download));
                form.ShowModel(download); Application.DoEvents();
                Check(form.ClientSize.Height > beforeModel && details.Bottom < details.Parent.Height);
                using (var bitmap = new Bitmap(form.Width, form.Height))
                {
                    form.DrawToBitmap(bitmap, new Rectangle(Point.Empty, bitmap.Size));
                    bitmap.Save(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "connector-model-preview.png"));
                }
                Check(ModelProgress.TryDecode(Encode(new Dictionary<string, object> { { "phase", "idle" }, { "filename", "" }, { "completed_bytes", 0 }, { "total_bytes", 0 } }), out download));
                form.ShowModel(download);
                Check(form.ClientSize.Height == beforeModel);
                int height = form.ClientSize.Height;
                details.PerformClick(); Application.DoEvents();
                Check(log.Visible && details.Text == "Hide details" && form.ClientSize.Height == height + 160);
                for (int i = 0; i < 100; i++) form.Append(new String('x', 500));
                Check(log.TextLength <= 24000 && log.TextLength > 500);
                details.PerformClick(); Application.DoEvents();
                Check(!log.Visible && form.ClientSize.Height == height);
                close.PerformClick();
                Check(!open.Enabled && !close.Enabled && connect.Enabled);
                form.ClientSize = new Size(1000, 620); Application.DoEvents();
                Check(form.Controls.Find("address", true)[0].Width > 500);
                form.Close();
            }
            using (var locked = new FileStream(Path.Combine(folder, item.Id + ".dat.lock"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None))
            {
                bool rejected = false;
                try { item.Remove(root); } catch (IOException) { rejected = true; }
                Check(rejected && Workspace.Load(root).Count == 1);
            }
            item.Remove(root);
            Check(Workspace.Load(root).Count == 0 && !File.Exists(Path.Combine(folder, item.Id + ".dat")) && !File.Exists(Path.Combine(folder, item.Id + ".json")));
            Check(File.Exists(Path.Combine(folder, "wrong-name.json")));
        }
        finally { if (Directory.Exists(root)) Directory.Delete(root, true); }
    }

    private static void TestLegacyMigration()
    {
        string root = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "legacy-workspace-" + Guid.NewGuid().ToString("N"));
        var value = new Dictionary<string, object> { { "version", 1 }, { "endpoint", "fixture123456" }, { "key", new String('A', 40) },
            { "ssh_key", "-----BEGIN OPENSSH PRIVATE KEY-----\n" + new String('A', 80) + "\n-----END OPENSSH PRIVATE KEY-----\n" } };
        string original = Encode(value);
        try
        {
            Storage.ProtectDirectory(root);
            string env = Path.Combine(root, ".env");
            File.WriteAllText(env, "OTHER_SETTING=preserved\nHOSTED_COMFYUI_ACCESS=" + original + "\n");
            using (var form = new ConnectorForm(root))
            {
                form.StartPosition = FormStartPosition.Manual; form.Location = new Point(-32000, -32000); form.ShowInTaskbar = false; form.Opacity = 0;
                form.Show(); Application.DoEvents();
                var list = (ListBox)form.Controls.Find("savedWorkspaces", true)[0];
                Check(list.Items.Count == 1 && form.Controls.Find("connect", true)[0].Enabled);
                Check(((Workspace)list.SelectedItem).Name.Contains("fixture123456"));
                using (var image = new Bitmap(form.Width, form.Height))
                {
                    form.DrawToBitmap(image, new Rectangle(Point.Empty, image.Size));
                    image.Save(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "connector-legacy-preview.png"));
                }
                form.Close();
            }
            Workspace saved = Workspace.Load(root)[0];
            Check(Invitation.Version(saved.Bookmark) == 3 && Invitation.SavedBookmark(saved.Bookmark) == saved.Bookmark);
            Check(Invitation.Identity(original) == Invitation.Identity(saved.Bookmark));
            Reject(delegate { Invitation.DecodeUri("hosted-comfyui://connect#" + saved.Bookmark); });
            Check(File.ReadAllText(env).Contains("OTHER_SETTING=preserved") && !File.ReadAllText(env).Contains(original));
            var bookmark = Invitation.Fields(saved.Bookmark);
            Check(bookmark.Count == 2 && !bookmark.ContainsKey("key") && !bookmark.ContainsKey("ssh_key"));
            string path = Path.Combine(root, "workspaces", saved.Id + ".dat");
            byte[] cipher = File.ReadAllBytes(path);
            Check(!Encoding.UTF8.GetString(cipher).Contains((string)value["key"]) && !Encoding.UTF8.GetString(cipher).Contains("OPENSSH PRIVATE KEY"));
            byte[] plain = ProtectedData.Unprotect(cipher, null, DataProtectionScope.CurrentUser);
            var reopened = ReadJson(Encoding.UTF8.GetString(plain)); Array.Clear(plain, 0, plain.Length);
            Check((string)reopened["key"] == (string)value["key"] && (string)reopened["ssh_key"] == (string)value["ssh_key"]);
            var descriptor = ReadJson(File.ReadAllText(Path.ChangeExtension(path, ".json")));
            Check(descriptor.Count == 3 && !descriptor.ContainsKey("key") && !descriptor.ContainsKey("ssh_key") && !descriptor.ContainsKey("endpoint"));
            Workspace.ImportLegacy(root);
            Check(Workspace.Load(root).Count == 1);

            string module = Path.Combine(root, "access.ps1");
            using (Stream input = typeof(ConnectorTests).Assembly.GetManifestResourceStream("access.ps1"))
            using (Stream output = File.Create(module)) input.CopyTo(output);
            string script = Path.Combine(root, "roundtrip.ps1");
            File.WriteAllText(Path.Combine(root, "bookmark.json"), new JavaScriptSerializer().Serialize(bookmark));
            File.WriteAllText(script, "$ErrorActionPreference='Stop'\n. (Join-Path $PSScriptRoot 'access.ps1')\n" +
                "function Invoke-WorkspaceRequest { throw 'Networking is not allowed in this test' }\n" +
                "$bookmark = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'bookmark.json') -Raw | ConvertFrom-Json\n" +
                "$access = Open-LegacyWorkspace $bookmark $PSScriptRoot\n" +
                "if ($access.endpoint -cne 'fixture123456' -or $access.key -cne ('A'*40)) { throw 'Native vault was not readable' }\n" +
                "$access.endpoint = 'fixture654321'\nOpen-LegacyWorkspace $access $PSScriptRoot | Out-Null\n");
            using (var process = Process.Start(new ProcessStartInfo(Program.PowerShell, "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File " + Storage.Quote(script))
                { UseShellExecute = false, CreateNoWindow = true, RedirectStandardError = true, RedirectStandardOutput = true }))
            {
                Check(process.WaitForExit(10000));
                Check(process.ExitCode == 0);
            }
            var entries = Workspace.Load(root);
            Check(entries.Count == 2);
            Workspace other = entries.Find(item => item.Identity != saved.Identity);
            byte[] fromScript = ProtectedData.Unprotect(File.ReadAllBytes(Path.Combine(root, "workspaces", other.Id + ".dat")), null, DataProtectionScope.CurrentUser);
            Check((string)ReadJson(Encoding.UTF8.GetString(fromScript))["endpoint"] == "fixture654321");
            Array.Clear(fromScript, 0, fromScript.Length);
            other.Remove(root);
            Check(File.ReadAllText(env).Contains(saved.Bookmark));
            saved.Remove(root);
            Check(!File.ReadAllText(env).Contains("HOSTED_COMFYUI_ACCESS") && File.ReadAllText(env).Contains("OTHER_SETTING=preserved"));
            using (var empty = new ConnectorForm(root))
            {
                empty.StartPosition = FormStartPosition.Manual; empty.Location = new Point(-32000, -32000); empty.ShowInTaskbar = false; empty.Opacity = 0;
                empty.Show(); Application.DoEvents();
                Check(((ListBox)empty.Controls.Find("savedWorkspaces", true)[0]).Items.Count == 0 && !empty.Controls.Find("connect", true)[0].Enabled);
                Check(!empty.Controls.Find("savedWorkspaces", true)[0].Visible && empty.Controls.Find("emptyWorkspaces", true)[0].Visible);
                using (var image = new Bitmap(empty.Width, empty.Height))
                {
                    empty.DrawToBitmap(image, new Rectangle(Point.Empty, image.Size));
                    image.Save(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "connector-empty-preview.png"));
                }
                empty.Close();
            }
            bookmark["workspace_id"] = "../../invalid";
            Reject(delegate { Invitation.Normalize(Encode(bookmark)); });
            bookmark["workspace_id"] = new String('a', 64); bookmark["key"] = "unexpected";
            Reject(delegate { Invitation.Normalize(Encode(bookmark)); });
        }
        finally { if (Directory.Exists(root)) Directory.Delete(root, true); }
    }

    private static void TestLiveStatus()
    {
        string root = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "live-status-" + Guid.NewGuid().ToString("N"));
        string first = new String('a', 32), second = new String('b', 32), third = new String('c', 32);
        try
        {
            var live = new LiveStatus(root);
            Check(LiveStatus.Read(root, first) == null && LiveStatus.Read(root, "../x") == null);
            live.Start(first, false);
            Check((string)LiveStatus.Read(root, first)["state"] == "starting");
            live.Observe("[12:34:56] Downloading Notch plugin");
            Check((string)LiveStatus.Read(root, first)["message"] == "Downloading Notch plugin");
            live.Observe("[12:34:56] private-key-secret");
            Check((string)LiveStatus.Read(root, first)["message"] == "Downloading Notch plugin");
            live.Add(second);
            live.Set("connected", "secret address");
            var status = LiveStatus.Read(root, first);
            Check(status.Count == 3 && (string)status["message"] == "Connected" && (string)status["address"] == LiveStatus.Address);
            Check((string)LiveStatus.Read(root, second)["state"] == "connected");
            var model = new Dictionary<string, object> { { "phase", "downloading" }, { "filename", "models/checkpoints/\u6a21\u578b-\u00e4.safetensors" },
                { "completed_bytes", 5368709120L }, { "total_bytes", 10737418240L } };
            ModelProgress progress;
            Check(ModelProgress.TryDecode(Encode(model), out progress));
            live.SetModel(progress);
            var snapshotModel = (Dictionary<string, object>)LiveStatus.Read(root, first)["model"];
            Check((string)snapshotModel["filename"] == (string)model["filename"] && (long)snapshotModel["completed_bytes"] == 5368709120L);
            TestSessionHttp(root, first, model);
            model["phase"] = "error"; model["error"] = "private-key-secret";
            Check(ModelProgress.TryDecode(Encode(model), out progress));
            live.SetModel(progress);
            Check(!new JavaScriptSerializer().Serialize(LiveStatus.Read(root, first)).Contains("private-key-secret"));
            Check((string)((Dictionary<string, object>)LiveStatus.Read(root, first)["model"])["error"] == "Model download failed. See ComfyUI for details.");
            model["phase"] = "idle";
            Check(ModelProgress.TryDecode(Encode(model), out progress));
            live.SetModel(progress);
            Check(!LiveStatus.Read(root, first).ContainsKey("model"));

            live.Observe("Connection failed: private-key-secret");
            live.Ended();
            Check((string)LiveStatus.Read(root, first)["state"] == "error" && !(string.IsNullOrEmpty((string)LiveStatus.Read(root, first)["message"])));
            Check(!new JavaScriptSerializer().Serialize(LiveStatus.Read(root, first)).Contains("private-key-secret"));
            foreach (string error in new[] { "This invitation has already been used on another device. Ask the owner for a new invitation.",
                "This invitation has expired. Ask the owner for a new invitation.",
                "This workspace is not saved on this computer. Open a new invitation from the owner." })
            {
                live.Observe("Connection failed: " + error);
                Check((string)LiveStatus.Read(root, first)["message"] == error);
            }
            live.Start(first, false);
            Check((string)LiveStatus.Read(root, second)["state"] == "starting");
            live.Set("connected", "");
            live.Enrolled();
            Check((bool)LiveStatus.Read(root, first)["enrolled"]);
            live.Ended();
            live.Start(third, true);
            live.Set("connected", "");
            Check((string)LiveStatus.Read(root, first)["state"] == "disconnected" && (string)LiveStatus.Read(root, second)["state"] == "disconnected");
            Check((string)LiveStatus.Read(root, third)["state"] == "connected");
            Check(!LiveStatus.Read(root, third).ContainsKey("enrolled") && (bool)LiveStatus.Read(root, first)["enrolled"]);

            string path = Path.Combine(root, "page-sessions", third + ".json");
            string valid = File.ReadAllText(path);
            var record = ReadJson(valid);
            record["process_start"] = (long)record["process_start"] + 1;
            WriteJson(path, record);
            Check((string)LiveStatus.Read(root, third)["state"] == "disconnected" && (string)LiveStatus.Read(root, third)["address"] == "");
            record = ReadJson(valid); record["process_id"] = Int32.MaxValue;
            WriteJson(path, record);
            Check((string)LiveStatus.Read(root, third)["state"] == "disconnected");
            record = ReadJson(valid); record["updated_at"] = DateTime.UtcNow.AddSeconds(-16).Ticks;
            WriteJson(path, record);
            Check((string)LiveStatus.Read(root, third)["state"] == "disconnected");
            record["updated_at"] = DateTime.UtcNow.AddMinutes(-31).Ticks;
            WriteJson(path, record);
            Check(LiveStatus.Read(root, third) == null);
            record["updated_at"] = DateTime.UtcNow.AddMinutes(2).Ticks;
            WriteJson(path, record);
            Check(LiveStatus.Read(root, third) == null);
            File.WriteAllText(path, "{broken");
            Check(LiveStatus.Read(root, third) == null);
            File.WriteAllText(path, new String('a', 16385));
            Check(LiveStatus.Read(root, third) == null);
            live.Heartbeat();
            Check((string)LiveStatus.Read(root, third)["state"] == "connected");

            Exception failure = null;
            var writer = new Thread(delegate() { try { for (int i = 0; i < 20; i++) live.Heartbeat(); } catch (Exception error) { failure = error; } });
            writer.Start();
            for (int i = 0; i < 30; i++)
            {
                var concurrent = LiveStatus.Read(root, third);
                Check(concurrent == null || (string)concurrent["state"] == "connected");
            }
            Check(writer.Join(5000) && failure == null);
            Check((string)LiveStatus.Read(root, third)["state"] == "connected");
            live.Clear();
            Check((string)LiveStatus.Read(root, third)["state"] == "disconnected");
        }
        finally { if (Directory.Exists(root)) Directory.Delete(root, true); }
    }

    private static void WriteJson(string path, Dictionary<string, object> value)
    {
        File.WriteAllText(path, new JavaScriptSerializer().Serialize(value));
    }

    private static void TestSessionHttp(string root, string session, Dictionary<string, object> model)
    {
        const string origin = "https://jkaarlehto.github.io";
        string nonce = new String('d', 32);
        string request = "GET /session?session=" + session + "&nonce=" + nonce + " HTTP/1.1\r\nHost: 127.0.0.1:18187\r\nOrigin: " + origin + "\r\n\r\n";
        Func<string, Dictionary<string, object>> reader = delegate(string token) { return LiveStatus.Read(root, token); };
        string preflight = request.Replace("GET ", "OPTIONS ").Replace("\r\n\r\n", "\r\nAccess-Control-Request-Method: GET\r\nAccess-Control-Request-Private-Network: true\r\n\r\n");
        Check(Presence.Response(preflight, origin, true, reader).StartsWith("HTTP/1.1 204"));
        foreach (string invalid in new[] { request.Replace(origin, "https://evil.example"), request.Replace("127.0.0.1:18187", "evil.example:18187"),
            request.Replace("GET ", "POST "), request.Replace(session, session.ToUpperInvariant()), request.Replace("&nonce=", "&session=" + session + "&nonce="),
            request.Replace(nonce, nonce + "&key=secret"), request.Replace("\r\n\r\n", "\r\nCookie: secret\r\n\r\n") })
        {
            string rejected = Presence.Response(invalid, origin, true, reader);
            Check(rejected.StartsWith("HTTP/1.1 4") && !rejected.Contains("Access-Control-Allow-Origin") && !rejected.Contains("secret"));
        }
        string unknown = Presence.Response(request.Replace(session, new String('e', 32)), origin, true, reader);
        Check(unknown.StartsWith("HTTP/1.1 404") && unknown.Contains("Access-Control-Allow-Origin: " + origin));
        using (var stop = new ManualResetEvent(false))
        using (var ready = new ManualResetEvent(false))
        {
            var listener = new TcpListener(IPAddress.Loopback, 0);
            Exception failure = null;
            var thread = new Thread(delegate()
            {
                try { Presence.Listen(listener, origin, stop, ready, delegate { return true; }, reader); }
                catch (Exception error) { failure = error; }
            });
            thread.Start();
            try
            {
                Check(ready.WaitOne(3000));
                int port = ((IPEndPoint)listener.LocalEndpoint).Port;
                string response = Probe(port, request);
                Check(response.StartsWith("HTTP/1.1 200"));
                string body = response.Substring(response.IndexOf("\r\n\r\n") + 4);
                Check(response.Contains("Content-Length: " + Encoding.UTF8.GetByteCount(body) + "\r\n"));
                var value = ReadJson(body);
                Check(value.Count == 8 && (string)value["app"] == "hosted-comfyui-connector" && (int)value["protocol"] == 1 &&
                    (string)value["nonce"] == nonce && (string)value["session"] == session && (string)value["state"] == "connected");
                Check((string)((Dictionary<string, object>)value["model"])["filename"] == (string)model["filename"]);
                Check(!body.Contains("process_id") && !body.Contains("process_start") && !body.Contains("ssh_key"));
                Check(Probe(port, preflight).StartsWith("HTTP/1.1 204"));
                Check(Probe(port, request.Replace(session, new String('e', 32))).StartsWith("HTTP/1.1 404"));
                Check(Probe(port, request.Replace(origin, "https://evil.example")).StartsWith("HTTP/1.1 403"));
            }
            finally { stop.Set(); Check(thread.Join(3000) && failure == null); }
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

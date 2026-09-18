using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Reflection;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using Microsoft.Win32;

namespace HostedComfyUI
{
    internal static class Presence
    {
        internal const int Port = 18187;
        internal const string Version = "1.1.0";
        private const string RunKey = @"Software\Microsoft\Windows\CurrentVersion\Run";
        private const string RunValue = "HostedComfyUIConnector";
        private static readonly string Name = @"Local\HostedComfyUI-Presence-" + WindowsIdentity.GetCurrent().User.Value;
        private static string Command { get { return Storage.Quote(Installer.ExePath) + " --presence"; } }

        internal static string OriginFromSite(string site)
        {
            Uri uri;
            if (!Uri.TryCreate(site, UriKind.Absolute, out uri) || uri.Scheme != "https" ||
                uri.UserInfo.Length != 0 || uri.Query.Length != 0 || uri.Fragment.Length != 0) return null;
            return uri.GetLeftPart(UriPartial.Authority);
        }

        private static string Origin()
        {
            using (Stream stream = Assembly.GetExecutingAssembly().GetManifestResourceStream("site.txt"))
            using (var reader = new StreamReader(stream)) return OriginFromSite(reader.ReadToEnd().Trim());
        }

        internal static bool RegistrationMatches(string command, string executable, bool exists)
        {
            return exists && String.Equals(command, Storage.Quote(executable) + " \"%1\"", StringComparison.OrdinalIgnoreCase);
        }

        internal static bool IsInstalled()
        {
            try
            {
                using (RegistryKey command = Registry.ClassesRoot.OpenSubKey(@"hosted-comfyui\shell\open\command"))
                    return RegistrationMatches(command == null ? null : command.GetValue("") as string, Installer.ExePath, File.Exists(Installer.ExePath));
            }
            catch (System.Security.SecurityException) { return false; }
            catch (UnauthorizedAccessException) { return false; }
            catch (IOException) { return false; }
        }

        internal static void RegisterStartup()
        {
            if (Origin() == null) { RemoveStartup(); return; }
            using (RegistryKey key = Registry.CurrentUser.CreateSubKey(RunKey))
            {
                string existing = key.GetValue(RunValue) as string;
                if (!String.IsNullOrEmpty(existing) && !String.Equals(existing, Command, StringComparison.OrdinalIgnoreCase))
                    throw new InvalidOperationException("The connector startup entry belongs to another application.");
                key.SetValue(RunValue, Command);
            }
        }

        internal static void RemoveStartup()
        {
            using (RegistryKey key = Registry.CurrentUser.OpenSubKey(RunKey, true))
                if (key != null && String.Equals(key.GetValue(RunValue) as string, Command, StringComparison.OrdinalIgnoreCase))
                    key.DeleteValue(RunValue, false);
        }

        private static EventWaitHandle CreateEvent(string suffix)
        {
            var acl = new EventWaitHandleSecurity();
            acl.SetAccessRuleProtection(true, false);
            acl.AddAccessRule(new EventWaitHandleAccessRule(WindowsIdentity.GetCurrent().User, EventWaitHandleRights.FullControl, AccessControlType.Allow));
            bool created;
            return new EventWaitHandle(false, EventResetMode.ManualReset, Name + suffix, out created, acl);
        }

        internal static void Start()
        {
            if (Origin() == null) return;
            if (!IsInstalled()) throw new InvalidOperationException("The connector protocol is not registered correctly.");
            using (Process process = Process.Start(new ProcessStartInfo(Installer.ExePath, "--presence") { UseShellExecute = false, CreateNoWindow = true }))
            {
                for (int attempt = 0; attempt < 50; attempt++)
                {
                    try
                    {
                        using (EventWaitHandle ready = EventWaitHandle.OpenExisting(Name + "-ready", EventWaitHandleRights.Synchronize))
                            if (ready.WaitOne(0)) return;
                    }
                    catch (WaitHandleCannotBeOpenedException) { }
                    if (process.HasExited && process.ExitCode != 0) break;
                    Thread.Sleep(100);
                }
            }
            throw new InvalidOperationException("The background connector could not start. Check whether local port 18187 is already in use, then reopen the installer.");
        }

        internal static void Stop()
        {
            for (int attempt = 0; attempt < 70; attempt++)
            {
                Mutex mutex;
                if (!Mutex.TryOpenExisting(Name, out mutex)) return;
                mutex.Dispose();
                try
                {
                    using (EventWaitHandle stop = EventWaitHandle.OpenExisting(Name + "-stop", EventWaitHandleRights.Modify)) stop.Set();
                }
                catch (WaitHandleCannotBeOpenedException) { }
                Thread.Sleep(100);
            }
            throw new InvalidOperationException("The background connector did not stop. Close it before updating or uninstalling.");
        }

        internal static int Run()
        {
            string origin = Origin();
            if (origin == null) return 0;
            if (!IsInstalled() || !String.Equals(System.Windows.Forms.Application.ExecutablePath, Installer.ExePath, StringComparison.OrdinalIgnoreCase)) return 1;
            var acl = new MutexSecurity();
            acl.SetAccessRuleProtection(true, false);
            acl.AddAccessRule(new MutexAccessRule(WindowsIdentity.GetCurrent().User, MutexRights.FullControl, AccessControlType.Allow));
            bool created;
            using (var mutex = new Mutex(true, Name, out created, acl))
            {
                if (!created) return 0;
                using (EventWaitHandle stop = CreateEvent("-stop"))
                using (EventWaitHandle ready = CreateEvent("-ready"))
                {
                    stop.Reset(); ready.Reset();
                    try
                    {
                        var listener = new TcpListener(IPAddress.Loopback, Port);
                        listener.ExclusiveAddressUse = true;
                        Listen(listener, origin, stop, ready, IsInstalled);
                    }
                    finally { ready.Reset(); mutex.ReleaseMutex(); }
                }
            }
            return 0;
        }

        internal static void Listen(TcpListener listener, string origin, WaitHandle stop, EventWaitHandle ready, Func<bool> installed,
            Func<string, Dictionary<string, object>> sessionReader = null)
        {
            var clients = new List<TcpClient>();
            var slots = new Semaphore(4, 4);
            try
            {
                listener.Start(8);
                ready.Set();
                var registered = Stopwatch.StartNew();
                while (!stop.WaitOne(50))
                {
                    if (registered.ElapsedMilliseconds >= 1000)
                    {
                        if (!installed()) break;
                        registered.Restart();
                    }
                    if (!listener.Pending()) continue;
                    TcpClient client = listener.AcceptTcpClient();
                    if (!slots.WaitOne(0)) { client.Close(); continue; }
                    lock (clients) clients.Add(client);
                    ThreadPool.QueueUserWorkItem(delegate
                    {
                        try { Handle(client, origin, installed, sessionReader); }
                        catch (IOException) { }
                        catch (SocketException) { }
                        catch (ObjectDisposedException) { }
                        finally { client.Close(); lock (clients) clients.Remove(client); slots.Release(); }
                    });
                }
            }
            finally
            {
                listener.Stop();
                lock (clients) foreach (TcpClient client in clients) client.Close();
                for (int i = 0; i < 4; i++) slots.WaitOne();
                slots.Dispose();
            }
        }

        private static void Handle(TcpClient client, string origin, Func<bool> installed,
            Func<string, Dictionary<string, object>> sessionReader)
        {
            using (NetworkStream stream = client.GetStream())
            {
                stream.WriteTimeout = 1000;
                var data = new byte[8192];
                int count = 0;
                var clock = Stopwatch.StartNew();
                while (count < data.Length && clock.ElapsedMilliseconds < 2000)
                {
                    stream.ReadTimeout = Math.Max(1, 2000 - (int)clock.ElapsedMilliseconds);
                    int read = stream.Read(data, count, data.Length - count);
                    if (read == 0) return;
                    count += read;
                    string request = Encoding.ASCII.GetString(data, 0, count);
                    if (request.IndexOf("\r\n\r\n", StringComparison.Ordinal) < 0) continue;
                    for (int i = 0; i < count; i++) if (data[i] >= 128) return;
                    byte[] response = Encoding.UTF8.GetBytes(Response(request, origin, installed(), sessionReader));
                    stream.Write(response, 0, response.Length);
                    return;
                }
            }
        }

        internal static string Response(string request, string origin, bool installed,
            Func<string, Dictionary<string, object>> sessionReader = null)
        {
            if (request == null || request.Length > 8192 || !request.EndsWith("\r\n\r\n", StringComparison.Ordinal)) return Error(400);
            string[] lines = request.Substring(0, request.Length - 4).Split(new[] { "\r\n" }, StringSplitOptions.None);
            string[] first = lines[0].Split(' ');
            if (first.Length != 3 || first[2] != "HTTP/1.1") return Error(400);
            var headers = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            for (int i = 1; i < lines.Length; i++)
            {
                int colon = lines[i].IndexOf(':');
                if (colon <= 0 || !Regex.IsMatch(lines[i].Substring(0, colon), @"\A[A-Za-z0-9-]+\z") ||
                    Regex.IsMatch(lines[i], @"[^\x20-\x7E]")) return Error(400);
                string key = lines[i].Substring(0, colon);
                if (headers.ContainsKey(key)) return Error(400);
                headers.Add(key, lines[i].Substring(colon + 1).Trim());
            }
            string host, suppliedOrigin;
            if (String.IsNullOrEmpty(origin) || !headers.TryGetValue("Host", out host) || host != "127.0.0.1:18187" ||
                !headers.TryGetValue("Origin", out suppliedOrigin) || suppliedOrigin != origin) return Error(403);
            string length;
            if (headers.ContainsKey("Transfer-Encoding") || headers.ContainsKey("Expect") || headers.ContainsKey("Authorization") ||
                headers.ContainsKey("Proxy-Authorization") || headers.ContainsKey("Cookie") ||
                (headers.TryGetValue("Content-Length", out length) && length != "0")) return Error(400);
            Match target = Regex.Match(first[1], @"\A/status\?nonce=([0-9a-f]{32})\z");
            Match session = Regex.Match(first[1], @"\A/session\?session=([0-9a-f]{32})&nonce=([0-9a-f]{32})\z");
            if (!target.Success && !session.Success) return Error(404);
            if (first[0] != "GET" && first[0] != "OPTIONS") return Error(405);
            if (!installed) return Error(404);
            string cors = "Access-Control-Allow-Origin: " + origin + "\r\nVary: Origin\r\n";
            if (first[0] == "OPTIONS")
            {
                string method, requestedHeaders, privateNetwork;
                if (!headers.TryGetValue("Access-Control-Request-Method", out method) || method != "GET" ||
                    (headers.TryGetValue("Access-Control-Request-Headers", out requestedHeaders) && requestedHeaders.Length != 0) ||
                    (headers.TryGetValue("Access-Control-Request-Private-Network", out privateNetwork) && privateNetwork != "true")) return Error(403);
                return "HTTP/1.1 204 No Content\r\n" + cors + "Access-Control-Allow-Methods: GET\r\nAccess-Control-Allow-Private-Network: true\r\nCache-Control: no-store\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";
            }
            string body;
            if (session.Success)
            {
                Dictionary<string, object> status = sessionReader == null
                    ? LiveStatus.Read(Storage.Root, session.Groups[1].Value) : sessionReader(session.Groups[1].Value);
                if (status == null) return Error(404, cors);
                status["app"] = "hosted-comfyui-connector"; status["protocol"] = 1;
                status["nonce"] = session.Groups[2].Value; status["session"] = session.Groups[1].Value;
                body = new JavaScriptSerializer().Serialize(status);
            }
            else body = "{\"app\":\"hosted-comfyui-connector\",\"protocol\":1,\"version\":\"" + Version + "\",\"live_status\":1,\"enrollment\":2,\"nonce\":\"" + target.Groups[1].Value + "\"}";
            return "HTTP/1.1 200 OK\r\n" + cors + "Content-Type: application/json; charset=utf-8\r\nCache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\nContent-Length: " + Encoding.UTF8.GetByteCount(body) + "\r\nConnection: close\r\n\r\n" + body;
        }

        private static string Error(int status, string cors = "")
        {
            return "HTTP/1.1 " + status + " Rejected\r\n" + cors + "Cache-Control: no-store\r\nContent-Length: 0\r\nConnection: close\r\n\r\n";
        }
    }
}

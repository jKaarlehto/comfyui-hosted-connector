using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
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
            using (var rejectedInstall = Process.Start(new ProcessStartInfo(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "HostedComfyUIConnector.exe"), "--install unexpected")
                { UseShellExecute = false, CreateNoWindow = true, RedirectStandardError = true }))
            {
                Check(rejectedInstall.WaitForExit(5000));
                Check(rejectedInstall.ExitCode == 2);
                Check(rejectedInstall.StandardError.ReadToEnd().Contains("no additional arguments"));
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
}

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Pipes;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Win32;

namespace HostedComfyUI
{
    internal static class Invitation
    {
        internal static string DecodeUri(string value)
        {
            Uri uri;
            if (value == null || value.Length > 16000 || !Uri.TryCreate(value, UriKind.Absolute, out uri) ||
                uri.Scheme != "hosted-comfyui" || uri.Host != "connect" || uri.Port != -1 ||
                uri.UserInfo.Length != 0 || uri.Query.Length != 0 ||
                (uri.AbsolutePath != "" && uri.AbsolutePath != "/") || uri.Fragment.Length < 2)
                throw new ArgumentException("This is not a Hosted ComfyUI invitation link.");
            return Normalize(uri.Fragment.Substring(1));
        }

        internal static string Normalize(string encoded)
        {
            if (encoded == null || encoded.Length > 15000 ||
                !Regex.IsMatch(encoded, @"\A[A-Za-z0-9_+/\-]+={0,2}\z"))
                throw new ArgumentException("The invitation is invalid.");
            encoded = encoded.Replace('-', '+').Replace('_', '/');
            encoded = encoded.PadRight((encoded.Length + 3) / 4 * 4, '=');
            try
            {
                byte[] data = Convert.FromBase64String(encoded);
                string json = new UTF8Encoding(false, true).GetString(data);
                var fields = new JavaScriptSerializer().DeserializeObject(json) as Dictionary<string, object>;
                if (fields == null || fields.Count != 4 || !fields.ContainsKey("version") ||
                    !(fields["version"] is int) || (int)fields["version"] != 1 ||
                    !FieldMatches(fields, "endpoint", @"\A[a-z0-9]{8,40}\z") ||
                    !FieldMatches(fields, "key", @"\A[A-Za-z0-9_-]{20,200}\z") ||
                    !FieldMatches(fields, "ssh_key", @"\A-----BEGIN OPENSSH PRIVATE KEY-----\r?\n[A-Za-z0-9+/=\r\n]{40,8000}-----END OPENSSH PRIVATE KEY-----\s*\z"))
                    throw new FormatException();
                return Convert.ToBase64String(data);
            }
            catch (Exception error)
            {
                if (error is OutOfMemoryException) throw;
                throw new ArgumentException("The invitation is invalid or uses an unsupported version.");
            }
        }

        private static bool FieldMatches(Dictionary<string, object> fields, string key, string pattern)
        {
            object value;
            return fields.TryGetValue(key, out value) && value is string && Regex.IsMatch((string)value, pattern);
        }
    }

    internal static class Storage
    {
        internal static readonly string Root = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "HostedComfyUIConnector");

        internal static void ProtectDirectory(string path)
        {
            Directory.CreateDirectory(path);
            var acl = new DirectorySecurity();
            acl.SetAccessRuleProtection(true, false);
            acl.AddAccessRule(new FileSystemAccessRule(WindowsIdentity.GetCurrent().User,
                FileSystemRights.FullControl, InheritanceFlags.ContainerInherit | InheritanceFlags.ObjectInherit,
                PropagationFlags.None, AccessControlType.Allow));
            Directory.SetAccessControl(path, acl);
        }

        internal static string ExtractLauncher()
        {
            ProtectDirectory(Root);
            string path = Path.Combine(Root, "start_hosted_comfyui.ps1");
            using (Stream input = Assembly.GetExecutingAssembly().GetManifestResourceStream("launcher.ps1"))
            using (Stream output = File.Create(path)) input.CopyTo(output);
            return path;
        }

        internal static string Quote(string value)
        {
            if (value.IndexOf('"') >= 0 || value.IndexOf('\0') >= 0) throw new ArgumentException("Invalid path.");
            return "\"" + value.TrimEnd('\\') + "\"";
        }
    }

    internal static class Installer
    {
        private const string Protocol = @"Software\Classes\hosted-comfyui";
        private const string Uninstall = @"Software\Microsoft\Windows\CurrentVersion\Uninstall\HostedComfyUIConnector";
        internal static readonly string DirectoryPath = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "HostedComfyUIConnector");
        internal static readonly string ExePath = Path.Combine(DirectoryPath, "HostedComfyUIConnector.exe");
        private static readonly string Shortcut = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), "Hosted ComfyUI Connector.lnk");

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        private static extern int GetCurrentPackageFullName(ref int length, StringBuilder name);

        internal static bool IsPackaged()
        {
            int length = 0;
            return GetCurrentPackageFullName(ref length, null) != 15700;
        }

        internal static bool EnsureInstalled(bool hasInvitation)
        {
            if (IsPackaged()) return true;
            if (String.Equals(Application.ExecutablePath, ExePath, StringComparison.OrdinalIgnoreCase)) return true;
            if (MessageBox.Show("Install Hosted ComfyUI Connector for your Windows account?\n\nIt opens invitation links and keeps the local connection running. A small background helper starts at sign-in so the invitation page can detect it. It does not start a Pod or tunnel. No administrator access is needed.",
                "Hosted ComfyUI Connector", MessageBoxButtons.OKCancel, MessageBoxIcon.Information) != DialogResult.OK) return false;
            Install();
            return hasInvitation;
        }

        internal static void Install()
        {
            using (RegistryKey existing = Registry.ClassesRoot.OpenSubKey(@"hosted-comfyui\shell\open\command"))
            {
                string command = existing == null ? null : existing.GetValue("") as string;
                if (!String.IsNullOrEmpty(command) && command != Storage.Quote(ExePath) + " \"%1\"")
                    throw new InvalidOperationException("The hosted-comfyui protocol is already registered to another application.");
            }
            Presence.Stop();
            Directory.CreateDirectory(DirectoryPath);
            if (!String.Equals(Application.ExecutablePath, ExePath, StringComparison.OrdinalIgnoreCase))
            {
                for (int attempt = 0; ; attempt++)
                {
                    try { File.Copy(Application.ExecutablePath, ExePath, true); break; }
                    catch (IOException) { if (attempt == 19) throw; Thread.Sleep(100); }
                }
            }
            using (RegistryKey protocol = Registry.CurrentUser.CreateSubKey(Protocol))
            {
                protocol.SetValue("", "URL:Hosted ComfyUI Connector");
                protocol.SetValue("URL Protocol", "");
                using (RegistryKey command = protocol.CreateSubKey(@"shell\open\command")) command.SetValue("", Storage.Quote(ExePath) + " \"%1\"");
            }
            using (RegistryKey uninstall = Registry.CurrentUser.CreateSubKey(Uninstall))
            {
                uninstall.SetValue("DisplayName", "Hosted ComfyUI Connector");
                uninstall.SetValue("DisplayVersion", Presence.Version);
                uninstall.SetValue("Publisher", "ComfyUI-Notch");
                uninstall.SetValue("InstallLocation", DirectoryPath);
                uninstall.SetValue("UninstallString", Storage.Quote(ExePath) + " --uninstall");
                uninstall.SetValue("NoModify", 1);
                uninstall.SetValue("NoRepair", 1);
            }
            object shell = Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell"));
            object shortcut = shell.GetType().InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { Shortcut });
            shortcut.GetType().InvokeMember("TargetPath", BindingFlags.SetProperty, null, shortcut, new object[] { ExePath });
            shortcut.GetType().InvokeMember("Description", BindingFlags.SetProperty, null, shortcut, new object[] { "Connect to hosted ComfyUI" });
            shortcut.GetType().InvokeMember("Save", BindingFlags.InvokeMethod, null, shortcut, null);
            Marshal.ReleaseComObject(shortcut); Marshal.ReleaseComObject(shell);
            Presence.RegisterStartup();
            Presence.Start();
        }

        internal static void Remove()
        {
            if (MessageBox.Show("Remove Hosted ComfyUI Connector and its saved invitation?", "Hosted ComfyUI Connector",
                MessageBoxButtons.OKCancel, MessageBoxIcon.Question) != DialogResult.OK) return;
            using (RegistryKey command = Registry.CurrentUser.OpenSubKey(Protocol + @"\shell\open\command"))
            {
                if (command != null && (string)command.GetValue("") != Storage.Quote(ExePath) + " \"%1\"")
                    throw new InvalidOperationException("The protocol registration belongs to another application.");
            }
            Presence.Stop();
            Presence.RemoveStartup();
            Registry.CurrentUser.DeleteSubKeyTree(Protocol, false);
            Registry.CurrentUser.DeleteSubKeyTree(Uninstall, false);
            if (File.Exists(Shortcut)) File.Delete(Shortcut);
            if (Directory.Exists(Storage.Root)) Directory.Delete(Storage.Root, true);
            string cleanup = Path.Combine(Path.GetTempPath(), "hosted-comfyui-uninstall-" + Guid.NewGuid().ToString("N") + ".ps1");
            string script = "Start-Sleep -Seconds 2\nRemove-Item -LiteralPath '" + ExePath.Replace("'", "''") + "' -Force\nRemove-Item -LiteralPath '" + DirectoryPath.Replace("'", "''") + "'\nRemove-Item -LiteralPath $PSCommandPath -Force\n";
            File.WriteAllText(cleanup, script);
            Process.Start(new ProcessStartInfo(Program.PowerShell, "-NoProfile -ExecutionPolicy Bypass -File " + Storage.Quote(cleanup)) { UseShellExecute = false, CreateNoWindow = true });
        }
    }

    internal sealed class ChildJob : IDisposable
    {
        [StructLayout(LayoutKind.Sequential)] private struct BasicLimits
        {
            internal long ProcessTime, JobTime;
            internal uint Flags;
            internal UIntPtr MinimumWorkingSet, MaximumWorkingSet;
            internal uint ActiveProcesses;
            internal UIntPtr Affinity;
            internal uint Priority, Scheduling;
        }
        [StructLayout(LayoutKind.Sequential)] private struct ExtendedLimits
        {
            internal BasicLimits Basic;
            internal ulong ReadOperations, WriteOperations, OtherOperations, ReadBytes, WriteBytes, OtherBytes;
            internal UIntPtr ProcessMemory, JobMemory, PeakProcessMemory, PeakJobMemory;
        }
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)] private static extern IntPtr CreateJobObject(IntPtr attributes, string name);
        [DllImport("kernel32.dll")] private static extern bool SetInformationJobObject(IntPtr job, int infoClass, ref ExtendedLimits limits, int size);
        [DllImport("kernel32.dll")] private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
        [DllImport("kernel32.dll")] private static extern bool CloseHandle(IntPtr handle);
        private IntPtr handle;

        internal ChildJob()
        {
            handle = CreateJobObject(IntPtr.Zero, null);
            var limits = new ExtendedLimits();
            limits.Basic.Flags = 0x2000;
            if (handle == IntPtr.Zero || !SetInformationJobObject(handle, 9, ref limits, Marshal.SizeOf(limits)))
                throw new InvalidOperationException("Could not initialize connection cleanup.");
        }
        internal void Add(Process process)
        {
            if (!AssignProcessToJobObject(handle, process.Handle)) throw new InvalidOperationException("Could not manage the connection process.");
        }
        public void Dispose() { if (handle != IntPtr.Zero) { CloseHandle(handle); handle = IntPtr.Zero; } }
    }

    internal sealed class ModelProgress
    {
        internal string Phase, Filename, Error;
        internal long Completed, Total;
        internal int Percent { get { return Total > 0 ? (int)(100.0 * Completed / Total) : -1; } }
        internal string Text
        {
            get
            {
                if (Phase == "idle") return String.Empty;
                if (Phase == "error") return "Model download failed: " + Filename + "\r\n" + Error;
                if (Phase == "verifying") return "Verifying model: " + Filename;
                string size = String.Format("{0:N1} MB", Completed / 1048576.0);
                if (Total > 0) size += String.Format(" / {0:N1} MB ({1}%)", Total / 1048576.0, Percent);
                return "Downloading model: " + Filename + "\r\n" + size;
            }
        }
        internal static bool TryDecode(string encoded, out ModelProgress value)
        {
            value = null;
            if (encoded == null || encoded.Length > 12000) return false;
            try
            {
                string json = new UTF8Encoding(false, true).GetString(Convert.FromBase64String(encoded));
                var fields = new JavaScriptSerializer { MaxJsonLength = 8192 }.DeserializeObject(json) as Dictionary<string, object>;
                object phase, filename, completed, total, error;
                if (fields == null || !fields.TryGetValue("phase", out phase) || !(phase is string) ||
                    !fields.TryGetValue("filename", out filename) || !(filename is string) || ((string)filename).Length > 1024 ||
                    !fields.TryGetValue("completed_bytes", out completed) || !(completed is int || completed is long) ||
                    !fields.TryGetValue("total_bytes", out total) || !(total is int || total is long)) return false;
                if ((string)phase != "idle" && (string)phase != "downloading" && (string)phase != "verifying" && (string)phase != "error") return false;
                long done = Convert.ToInt64(completed), size = Convert.ToInt64(total);
                if (done < 0 || size < 0 || (size > 0 && done > size)) return false;
                string detail = fields.TryGetValue("error", out error) && error is string ? (string)error : String.Empty;
                if (detail.Length > 400) detail = detail.Substring(0, 400);
                value = new ModelProgress { Phase = (string)phase, Filename = Clean((string)filename),
                    Completed = done, Total = size, Error = Clean(detail) };
                return true;
            }
            catch (Exception) { return false; }
        }
        private static string Clean(string text) { return Regex.Replace(text, "[\\x00-\\x1f\\x7f]", " "); }
    }

    internal sealed class ConnectorForm : Form
    {
        private readonly TextBox log = new TextBox();
        private readonly LinkLabel link = new LinkLabel();
        private readonly Button reconnect = new Button();
        private readonly Panel modelPanel = new Panel();
        private readonly Label modelStatus = new Label();
        private readonly ProgressBar modelProgress = new ProgressBar();
        private Process process;
        private ChildJob job;
        private string activeInvitation;
        private string accessPath;
        private string stopPath;
        private string sessionPath;
        private bool closing;
        private const string LocalUrl = "http://127.0.0.1:18188";

        internal ConnectorForm()
        {
            Text = "Hosted ComfyUI Connector";
            ClientSize = new Size(620, 430);
            MinimumSize = new Size(500, 300);
            StartPosition = FormStartPosition.CenterScreen;
            log.Multiline = true; log.ReadOnly = true; log.ScrollBars = ScrollBars.Vertical;
            log.Dock = DockStyle.Fill; log.BackColor = SystemColors.Window;
            log.Text = "Open your invitation page and click Connect.\r\nKeep this window open while using hosted ComfyUI.\r\n";
            var footer = new FlowLayoutPanel { Dock = DockStyle.Bottom, Height = 42, Padding = new Padding(8) };
            link.Text = "Open ComfyUI: " + LocalUrl; link.AutoSize = true; link.Enabled = false; link.Margin = new Padding(0, 6, 12, 0);
            link.LinkClicked += delegate { OpenBrowser(); };
            reconnect.Text = "Reconnect"; reconnect.Enabled = false;
            reconnect.Click += delegate { Connect(activeInvitation); };
            footer.Controls.Add(link); footer.Controls.Add(reconnect);
            modelPanel.Dock = DockStyle.Bottom; modelPanel.Height = 82; modelPanel.Padding = new Padding(8); modelPanel.Visible = false;
            modelStatus.Dock = DockStyle.Fill; modelStatus.AutoEllipsis = true;
            modelProgress.Dock = DockStyle.Bottom; modelProgress.Height = 16;
            modelPanel.Controls.Add(modelStatus); modelPanel.Controls.Add(modelProgress);
            Controls.Add(log); Controls.Add(modelPanel); Controls.Add(footer);
            string saved = Path.Combine(Storage.Root, ".env");
            if (File.Exists(saved))
            {
                foreach (string line in File.ReadAllLines(saved))
                    if (line.StartsWith("HOSTED_COMFYUI_ACCESS=", StringComparison.Ordinal))
                        try { activeInvitation = Invitation.Normalize(line.Substring("HOSTED_COMFYUI_ACCESS=".Length)); reconnect.Enabled = true; } catch (ArgumentException) { }
            }
        }

        internal void Accept(string invitation)
        {
            if (closing) return;
            Show(); WindowState = FormWindowState.Normal; Activate();
            if (String.IsNullOrEmpty(invitation)) return;
            if (process != null && !process.HasExited)
            {
                if (activeInvitation == invitation) { if (link.Enabled) OpenBrowser(); return; }
                if (MessageBox.Show(this, "Replace the current connection with this invitation?", Text,
                    MessageBoxButtons.OKCancel, MessageBoxIcon.Question) != DialogResult.OK) return;
                Stop();
            }
            Connect(invitation);
        }

        private void Connect(string invitation)
        {
            try
            {
                Stop();
                activeInvitation = Invitation.Normalize(invitation);
                string script = Storage.ExtractLauncher();
                accessPath = Path.Combine(Storage.Root, "invitation-" + Guid.NewGuid().ToString("N"));
                stopPath = Path.Combine(Storage.Root, "stop-" + Guid.NewGuid().ToString("N"));
                sessionPath = Path.Combine(Storage.Root, "session-" + Guid.NewGuid().ToString("N"));
                File.WriteAllText(accessPath, activeInvitation, new UTF8Encoding(false));
                log.Clear(); link.Enabled = false; reconnect.Enabled = false; modelPanel.Visible = false;
                Text = "Hosted ComfyUI - Connecting";
                var info = new ProcessStartInfo(Program.PowerShell,
                    "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File " + Storage.Quote(script) +
                    " -AccessFile " + Storage.Quote(accessPath) + " -EnvFile " + Storage.Quote(Path.Combine(Storage.Root, ".env")) +
                    " -ConnectorMode -StopFile " + Storage.Quote(stopPath) + " -SessionDirectory " + Storage.Quote(sessionPath))
                { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
                job = new ChildJob();
                process = new Process { StartInfo = info, EnableRaisingEvents = true };
                process.OutputDataReceived += Output;
                process.ErrorDataReceived += Output;
                process.Exited += delegate(object sender, EventArgs args) { Post(delegate { if (!closing && Object.ReferenceEquals(sender, process)) { link.Enabled = false; reconnect.Enabled = true; modelPanel.Visible = false; Text = "Hosted ComfyUI - Disconnected"; Append("Connection ended. Click Reconnect to try again."); } }); };
                process.Start(); job.Add(process);
                process.BeginOutputReadLine(); process.BeginErrorReadLine();
            }
            catch (Exception error) { Stop(); Append("Could not connect: " + error.Message); reconnect.Enabled = activeInvitation != null; }
        }

        private void Output(object sender, DataReceivedEventArgs args)
        {
            if (args.Data == null) return;
            Post(delegate
            {
                if (!Object.ReferenceEquals(sender, process)) return;
                if (args.Data == "HOSTED_COMFYUI_READY=" + LocalUrl)
                {
                    link.Enabled = true; Text = "Hosted ComfyUI - Connected";
                    DeleteAccessFile(); OpenBrowser();
                }
                else if (args.Data.StartsWith("HOSTED_COMFYUI_STORAGE=", StringComparison.Ordinal))
                {
                    ModelProgress value;
                    if (link.Enabled && ModelProgress.TryDecode(args.Data.Substring("HOSTED_COMFYUI_STORAGE=".Length), out value))
                    {
                        modelStatus.Text = value.Text;
                        modelPanel.Visible = value.Phase != "idle";
                        modelProgress.Visible = value.Phase != "error";
                        modelProgress.Style = value.Percent < 0 || value.Phase == "verifying"
                            ? ProgressBarStyle.Marquee : ProgressBarStyle.Continuous;
                        modelProgress.Value = Math.Max(0, value.Percent);
                    }
                }
                else Append(args.Data);
            });
        }

        private void Post(Action action) { if (!closing && IsHandleCreated) { try { BeginInvoke(action); } catch (InvalidOperationException) { } } }
        private void Append(string text) { log.AppendText(text + Environment.NewLine); }
        private void OpenBrowser() { try { Process.Start(new ProcessStartInfo(LocalUrl) { UseShellExecute = true }); } catch { Append("Open " + LocalUrl + " in your browser."); } }
        private void DeleteAccessFile() { if (accessPath != null && File.Exists(accessPath)) File.Delete(accessPath); accessPath = null; }
        private void Stop()
        {
            if (process != null)
            {
                if (!process.HasExited && stopPath != null) { File.WriteAllText(stopPath, ""); process.WaitForExit(3000); }
                if (job != null) job.Dispose();
                if (!process.HasExited && !process.WaitForExit(3000)) { process.Kill(); process.WaitForExit(3000); }
                process.Dispose(); process = null; job = null;
            }
            DeleteAccessFile();
            if (stopPath != null && File.Exists(stopPath)) File.Delete(stopPath);
            if (sessionPath != null)
            {
                for (int attempt = 0; attempt < 10; attempt++)
                {
                    try { if (Directory.Exists(sessionPath)) Directory.Delete(sessionPath, true); break; }
                    catch (IOException) { Thread.Sleep(100); }
                    catch (UnauthorizedAccessException) { Thread.Sleep(100); }
                }
                if (Directory.Exists(sessionPath)) MessageBox.Show(this,
                    "The connection stopped, but Windows could not remove its protected temporary files:\n" + sessionPath,
                    "Hosted ComfyUI Connector", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
            stopPath = null;
            sessionPath = null;
        }
        protected override void OnFormClosing(FormClosingEventArgs args) { closing = true; Stop(); base.OnFormClosing(args); }
    }

    internal static class Program
    {
        internal static readonly string PowerShell = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Windows), @"System32\WindowsPowerShell\v1.0\powershell.exe");
        internal static readonly string InstanceName = @"Local\HostedComfyUIConnector-" + WindowsIdentity.GetCurrent().User.Value;

        [STAThread]
        private static void Main(string[] args)
        {
            if (args.Length > 0 && args[0] == "--presence")
            {
                if (args.Length != 1) { Environment.ExitCode = 2; return; }
                try { Environment.ExitCode = Presence.Run(); }
                catch (Exception) { Environment.ExitCode = 1; }
                return;
            }
            if (args.Length > 0 && args[0] == "--install")
            {
                if (args.Length != 1) { Console.Error.WriteLine("--install takes no additional arguments."); Environment.ExitCode = 2; return; }
                try
                {
                    Mutex running;
                    if (Mutex.TryOpenExisting(InstanceName, out running))
                    {
                        running.Dispose();
                        throw new InvalidOperationException("Close Hosted ComfyUI Connector before updating it.");
                    }
                    Installer.Install();
                }
                catch (Exception error) { Console.Error.WriteLine(error.Message); Environment.ExitCode = 1; }
                return;
            }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            try
            {
                string name = InstanceName.Substring("Local\\".Length);
                if (args.Length == 1 && args[0] == "--uninstall")
                {
                    Mutex running;
                    if (Mutex.TryOpenExisting(@"Local\" + name, out running))
                    {
                        running.Dispose();
                        throw new InvalidOperationException("Close Hosted ComfyUI Connector before uninstalling it.");
                    }
                    Installer.Remove(); return;
                }
                if (args.Length > 1) throw new ArgumentException("Open one invitation link at a time.");
                string invitation = args.Length == 1 ? Invitation.DecodeUri(args[0]) : "";
                bool created;
                using (var mutex = new Mutex(true, @"Local\" + name, out created))
                {
                    if (!created)
                    {
                        if (args.Length == 0 && !Installer.IsPackaged() &&
                            !String.Equals(Application.ExecutablePath, Installer.ExePath, StringComparison.OrdinalIgnoreCase))
                        {
                            MessageBox.Show("Close Hosted ComfyUI Connector, then reopen this installer to update it.",
                                "Hosted ComfyUI Connector", MessageBoxButtons.OK, MessageBoxIcon.Information);
                            return;
                        }
                        using (var client = new NamedPipeClientStream(".", name, PipeDirection.Out))
                        {
                            client.Connect(5000);
                            using (var writer = new StreamWriter(client)) writer.WriteLine(invitation);
                        }
                        return;
                    }
                    if (!Installer.EnsureInstalled(invitation.Length != 0)) return;
                    var form = new ConnectorForm();
                    var thread = new Thread(delegate() { Listen(name, form); });
                    thread.IsBackground = true;
                    form.Shown += delegate { thread.Start(); form.Accept(invitation); };
                    Application.Run(form);
                    mutex.ReleaseMutex();
                }
            }
            catch (Exception error) { MessageBox.Show(error.Message, "Hosted ComfyUI Connector", MessageBoxButtons.OK, MessageBoxIcon.Error); }
        }

        private static void Listen(string name, ConnectorForm form)
        {
            var security = new PipeSecurity();
            security.SetAccessRuleProtection(true, false);
            security.AddAccessRule(new PipeAccessRule(WindowsIdentity.GetCurrent().User, PipeAccessRights.FullControl, AccessControlType.Allow));
            while (true)
            {
                try
                {
                    using (var pipe = new NamedPipeServerStream(name, PipeDirection.In, 1, PipeTransmissionMode.Byte,
                        PipeOptions.None, 16000, 16000, security))
                    {
                        pipe.WaitForConnection();
                        using (var reader = new StreamReader(pipe))
                        {
                            var message = new StringBuilder();
                            int value;
                            while (message.Length <= 15000 && (value = reader.Read()) >= 0 && value != '\n') message.Append((char)value);
                            string invitation = message.ToString().TrimEnd('\r');
                            if (invitation.Length != 0) invitation = Invitation.Normalize(invitation);
                            form.BeginInvoke(new Action(delegate { form.Accept(invitation); }));
                        }
                    }
                }
                catch (ObjectDisposedException) { return; }
                catch (InvalidOperationException) { return; }
                catch (Exception) { }
            }
        }
    }
}

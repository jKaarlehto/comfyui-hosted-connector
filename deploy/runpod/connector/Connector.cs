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
            string session;
            return DecodeUri(value, out session);
        }

        internal static string DecodeUri(string value, out string session)
        {
            session = "";
            Uri uri;
            if (value == null || value.Length > 16000 || !Uri.TryCreate(value, UriKind.Absolute, out uri) ||
                uri.Scheme != "hosted-comfyui" || uri.Host != "connect" || uri.Port != -1 ||
                uri.UserInfo.Length != 0 || (uri.Query.Length != 0 && !Regex.IsMatch(uri.Query, @"\A\?session=[0-9a-f]{32}\z")) ||
                (uri.AbsolutePath != "" && uri.AbsolutePath != "/") || uri.Fragment.Length < 2)
                throw new ArgumentException("This is not a Hosted ComfyUI invitation link.");
            if (uri.Query.Length != 0) session = uri.Query.Substring("?session=".Length);
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
                if (fields == null || fields.Count != 4 || !fields.ContainsKey("version") || !(fields["version"] is int)) throw new FormatException();
                if ((int)fields["version"] == 1)
                {
                    if (!FieldMatches(fields, "endpoint", @"\A[a-z0-9]{8,40}\z") ||
                        !FieldMatches(fields, "key", @"\A[A-Za-z0-9_-]{20,200}\z") ||
                        !FieldMatches(fields, "ssh_key", @"\A-----BEGIN OPENSSH PRIVATE KEY-----\r?\n[A-Za-z0-9+/=\r\n]{40,8000}-----END OPENSSH PRIVATE KEY-----\s*\z")) throw new FormatException();
                }
                else if ((int)fields["version"] == 2)
                {
                    object gateway;
                    if (!fields.TryGetValue("gateway", out gateway) || !(gateway is string) || !ValidGateway((string)gateway) ||
                        !FieldMatches(fields, "invite_id", @"\A[0-9a-f]{32}\z") ||
                        !FieldMatches(fields, "token", @"\A(?:[0-9a-f]{64})?\z")) throw new FormatException();
                }
                else throw new FormatException();
                return Convert.ToBase64String(data);
            }
            catch (Exception error)
            {
                if (error is OutOfMemoryException) throw;
                throw new ArgumentException("The invitation is invalid or uses an unsupported version.");
            }
        }

        internal static bool ValidGateway(string value)
        {
            return value != null && Regex.IsMatch(value,
                @"\Ahttps://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.workers\.dev\z");
        }

        internal static string Identity(string encoded)
        {
            var fields = new JavaScriptSerializer().DeserializeObject(Encoding.UTF8.GetString(Convert.FromBase64String(encoded))) as Dictionary<string, object>;
            return (int)fields["version"] == 2 ? (string)fields["gateway"] + "|" + (string)fields["invite_id"] : encoded;
        }

        internal static string SavedBookmark(string encoded)
        {
            string normalized = Normalize(encoded);
            var fields = new JavaScriptSerializer().DeserializeObject(Encoding.UTF8.GetString(Convert.FromBase64String(normalized))) as Dictionary<string, object>;
            if ((int)fields["version"] != 2 || (string)fields["token"] != "") throw new ArgumentException("Invalid saved workspace.");
            return normalized;
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
            using (Stream input = Assembly.GetExecutingAssembly().GetManifestResourceStream("access.ps1"))
            using (Stream output = File.Create(Path.Combine(Root, "hosted_access.ps1"))) input.CopyTo(output);
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
        private static readonly string Shortcut = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), "ComfyUI Notch Connector.lnk");
        private static readonly string LegacyShortcut = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.Programs), "Hosted ComfyUI Connector.lnk");

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
            if (MessageBox.Show("Install ComfyUI Notch Connector for your Windows account?\n\nIt opens invitations, remembers your workspaces, and keeps the local connection running. A small background helper starts at sign-in so the invitation page can detect it. It does not start a Pod or tunnel. No administrator access is needed.",
                "ComfyUI Notch Connector", MessageBoxButtons.OKCancel, MessageBoxIcon.Information) != DialogResult.OK) return false;
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
                protocol.SetValue("", "URL:ComfyUI Notch Connector");
                protocol.SetValue("URL Protocol", "");
                using (RegistryKey command = protocol.CreateSubKey(@"shell\open\command")) command.SetValue("", Storage.Quote(ExePath) + " \"%1\"");
            }
            using (RegistryKey uninstall = Registry.CurrentUser.CreateSubKey(Uninstall))
            {
                uninstall.SetValue("DisplayName", "ComfyUI Notch Connector");
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
            RemoveLegacyShortcut();
            Presence.RegisterStartup();
            Presence.Start();
        }

        internal static void Remove()
        {
            if (MessageBox.Show("Remove ComfyUI Notch Connector and its saved workspaces?", "ComfyUI Notch Connector",
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
            RemoveLegacyShortcut();
            if (Directory.Exists(Storage.Root)) Directory.Delete(Storage.Root, true);
            string cleanup = Path.Combine(Path.GetTempPath(), "hosted-comfyui-uninstall-" + Guid.NewGuid().ToString("N") + ".ps1");
            string script = "Start-Sleep -Seconds 2\nRemove-Item -LiteralPath '" + ExePath.Replace("'", "''") + "' -Force\nRemove-Item -LiteralPath '" + DirectoryPath.Replace("'", "''") + "'\nRemove-Item -LiteralPath $PSCommandPath -Force\n";
            File.WriteAllText(cleanup, script);
            Process.Start(new ProcessStartInfo(Program.PowerShell, "-NoProfile -ExecutionPolicy Bypass -File " + Storage.Quote(cleanup)) { UseShellExecute = false, CreateNoWindow = true });
        }

        private static void RemoveLegacyShortcut()
        {
            if (!File.Exists(LegacyShortcut)) return;
            object shell = null, link = null;
            try
            {
                shell = Activator.CreateInstance(Type.GetTypeFromProgID("WScript.Shell"));
                link = shell.GetType().InvokeMember("CreateShortcut", BindingFlags.InvokeMethod, null, shell, new object[] { LegacyShortcut });
                string target = link.GetType().InvokeMember("TargetPath", BindingFlags.GetProperty, null, link, null) as string;
                if (String.Equals(target, ExePath, StringComparison.OrdinalIgnoreCase)) File.Delete(LegacyShortcut);
            }
            finally { if (link != null) Marshal.ReleaseComObject(link); if (shell != null) Marshal.ReleaseComObject(shell); }
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
        private readonly Button link = new Button();
        private readonly Button copy = new Button();
        private readonly Button disconnect = new Button();
        private readonly Button reconnect = new Button();
        private readonly Button details = new Button();
        private readonly Button remove = new Button();
        private readonly Label heading = new Label();
        private readonly Label summary = new Label();
        private readonly TextBox address = new TextBox();
        private readonly ListBox workspaces = new ListBox();
        private readonly Label noWorkspaces = new Label();
        private readonly ProgressBar activity = new ProgressBar();
        private readonly Panel modelPanel = new Panel();
        private readonly Label modelStatus = new Label();
        private readonly ProgressBar modelProgress = new ProgressBar();
        private readonly LiveStatus live;
        private readonly string storageRoot;
        private readonly System.Windows.Forms.Timer heartbeat = new System.Windows.Forms.Timer();
        private Process process;
        private ChildJob job;
        private string activeInvitation;
        private string accessPath;
        private string stopPath;
        private string sessionPath;
        private bool closing;
        private bool modelVisible;
        private string phaseState = "disconnected", phaseMessage = "Choose a saved workspace, or open an invitation to get started.";
        private const string LocalUrl = "http://127.0.0.1:18188";

        internal ConnectorForm(string directory = null)
        {
            SuspendLayout();
            storageRoot = directory ?? Storage.Root;
            live = new LiveStatus(storageRoot);
            Text = "ComfyUI Notch Connector";
            Font = new Font("Segoe UI", 10);
            BackColor = Color.White;
            ClientSize = new Size(660, 452);
            MinimumSize = new Size(560, 490);
            StartPosition = FormStartPosition.CenterScreen;
            var layout = new TableLayoutPanel { Dock = DockStyle.Fill, Padding = new Padding(24), ColumnCount = 1, RowCount = 9 };
            for (int i = 0; i < 8; i++) layout.RowStyles.Add(new RowStyle(SizeType.AutoSize));
            layout.RowStyles.Add(new RowStyle(SizeType.Percent, 100));
            var brand = new Label { Text = "ComfyUI Notch", AutoSize = true, ForeColor = Color.FromArgb(48, 59, 199), Font = new Font(Font, FontStyle.Bold), Margin = new Padding(0, 0, 0, 12) };
            heading.Name = "phase"; heading.AutoSize = true; heading.Font = new Font("Segoe UI", 19, FontStyle.Bold); heading.Margin = new Padding(0, 0, 0, 4);
            summary.Name = "summary"; summary.AutoSize = true; summary.MaximumSize = new Size(590, 0); summary.Margin = new Padding(0, 0, 0, 12); summary.ForeColor = Color.FromArgb(80, 84, 96);
            var status = new FlowLayoutPanel { Dock = DockStyle.Fill, AutoSize = true, FlowDirection = FlowDirection.TopDown, WrapContents = false, Margin = Padding.Empty };
            status.Controls.Add(heading); status.Controls.Add(summary);
            activity.Dock = DockStyle.Top; activity.Height = 4; activity.Style = ProgressBarStyle.Marquee; activity.Visible = false;
            status.Controls.Add(activity);
            var saved = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 2, RowCount = 2, Margin = new Padding(0, 0, 0, 14) };
            saved.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100)); saved.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            saved.Controls.Add(new Label { Text = "Saved workspaces", AutoSize = true, Margin = new Padding(0, 0, 0, 6) }, 0, 0);
            StyleButton(remove, "Remove", false); remove.Name = "removeWorkspace"; remove.Enabled = false;
            remove.Click += delegate { RemoveWorkspace(); };
            saved.Controls.Add(remove, 1, 0); saved.SetRowSpan(remove, 2);
            var savedList = new Panel { Dock = DockStyle.Fill, Height = 78, Margin = Padding.Empty };
            workspaces.Name = "savedWorkspaces"; workspaces.Dock = DockStyle.Fill; workspaces.BorderStyle = BorderStyle.FixedSingle; workspaces.IntegralHeight = false;
            workspaces.SelectedIndexChanged += delegate { remove.Enabled = workspaces.SelectedItem != null; UpdateConnectButton(); };
            workspaces.DoubleClick += delegate { ConnectSelected(); };
            noWorkspaces.Text = "Accept an invitation to save a workspace on this computer.";
            noWorkspaces.Dock = DockStyle.Fill; noWorkspaces.ForeColor = summary.ForeColor; noWorkspaces.Padding = new Padding(10); noWorkspaces.BackColor = Color.FromArgb(245, 246, 249);
            savedList.Controls.Add(workspaces); savedList.Controls.Add(noWorkspaces);
            saved.Controls.Add(savedList, 0, 1);
            var endpoint = new TableLayoutPanel { Dock = DockStyle.Top, AutoSize = true, ColumnCount = 3, Margin = new Padding(0, 0, 0, 12) };
            endpoint.ColumnStyles.Add(new ColumnStyle(SizeType.Percent, 100)); endpoint.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize)); endpoint.ColumnStyles.Add(new ColumnStyle(SizeType.AutoSize));
            address.Name = "address"; address.Text = LocalUrl; address.ReadOnly = true; address.Dock = DockStyle.Fill; address.Margin = new Padding(0, 5, 6, 0); address.BackColor = Color.FromArgb(245, 246, 249);
            StyleButton(copy, "Copy", false); copy.Name = "copyAddress"; copy.Click += delegate { try { Clipboard.SetText(LocalUrl); } catch { Append("Could not copy the address. " + LocalUrl); } };
            StyleButton(link, "Open ComfyUI", false); link.Name = "openComfyUI"; link.Click += delegate { OpenBrowser(); };
            endpoint.Controls.Add(address, 0, 0); endpoint.Controls.Add(copy, 1, 0); endpoint.Controls.Add(link, 2, 0);
            var actions = new FlowLayoutPanel { Dock = DockStyle.Top, AutoSize = true, Margin = Padding.Empty, WrapContents = false };
            StyleButton(reconnect, "Connect", true); reconnect.Name = "connect"; reconnect.Click += delegate { ConnectSelected(); };
            StyleButton(disconnect, "Disconnect", false); disconnect.Name = "disconnect";
            disconnect.Click += delegate { Stop(); SetPhase("disconnected", "Your saved workspace is ready to reconnect."); UpdateConnectButton(); };
            actions.Controls.Add(reconnect); actions.Controls.Add(disconnect);
            StyleButton(details, "Show details", false); details.Name = "details"; details.Margin = new Padding(0, 12, 0, 8);
            details.Click += delegate { log.Visible = !log.Visible; details.Text = log.Visible ? "Hide details" : "Show details"; ClientSize = new Size(ClientSize.Width, ClientSize.Height + (log.Visible ? 160 : -160)); };
            log.Name = "connectionLog";
            log.Multiline = true; log.ReadOnly = true; log.ScrollBars = ScrollBars.Vertical;
            log.Dock = DockStyle.Fill; log.BackColor = Color.FromArgb(245, 246, 249); log.BorderStyle = BorderStyle.FixedSingle; log.Visible = false; log.MinimumSize = new Size(0, 100);
            modelPanel.Dock = DockStyle.Top; modelPanel.Height = 72; modelPanel.Padding = new Padding(0, 6, 0, 10); modelPanel.Visible = false;
            modelStatus.Dock = DockStyle.Fill; modelStatus.AutoEllipsis = true;
            modelProgress.Dock = DockStyle.Bottom; modelProgress.Height = 8;
            modelPanel.Controls.Add(modelStatus); modelPanel.Controls.Add(modelProgress);
            layout.Controls.Add(brand, 0, 0); layout.Controls.Add(status, 0, 1); layout.Controls.Add(modelPanel, 0, 2);
            layout.Controls.Add(saved, 0, 3); layout.Controls.Add(endpoint, 0, 4); layout.Controls.Add(actions, 0, 5);
            layout.Controls.Add(details, 0, 6); layout.Controls.Add(new Panel { Height = 1, Dock = DockStyle.Top }, 0, 7); layout.Controls.Add(log, 0, 8);
            Controls.Add(layout);
            Resize += delegate { summary.MaximumSize = new Size(Math.Max(300, ClientSize.Width - 52), 0); activity.Width = Math.Max(300, ClientSize.Width - 52); };
            heartbeat.Interval = 2000;
            heartbeat.Tick += delegate { live.Heartbeat(); };
            heartbeat.Start();
            string savedEnv = Path.Combine(storageRoot, ".env");
            if (File.Exists(savedEnv))
            {
                foreach (string line in File.ReadAllLines(savedEnv))
                    if (line.StartsWith("HOSTED_COMFYUI_ACCESS=", StringComparison.Ordinal))
                        try { activeInvitation = Invitation.Normalize(line.Substring("HOSTED_COMFYUI_ACCESS=".Length)); } catch (ArgumentException) { }
            }
            RefreshWorkspaces();
            SetPhase("disconnected", "Choose a saved workspace, or open an invitation to get started.");
            heading.Text = "Ready to connect";
            Text = "ComfyUI Notch Connector";
            AutoScaleDimensions = new SizeF(96, 96);
            AutoScaleMode = AutoScaleMode.Dpi;
            ResumeLayout(true);
        }

        private static void StyleButton(Button button, string text, bool primary)
        {
            button.Text = text; button.AutoSize = true; button.Height = 34; button.MinimumSize = new Size(78, 34); button.Padding = new Padding(8, 0, 8, 0);
            button.FlatStyle = FlatStyle.Flat; button.FlatAppearance.BorderColor = Color.FromArgb(217, 220, 231);
            button.BackColor = primary ? Color.FromArgb(48, 59, 199) : Color.White; button.ForeColor = primary ? Color.White : Color.FromArgb(40, 44, 58);
            button.Margin = new Padding(0, 0, 8, 0);
        }

        internal void SetPhase(string state, string message)
        {
            phaseState = state; phaseMessage = message;
            heading.Text = state == "connected" ? "Connected" : state == "starting" ? "Connecting" : state == "error" ? "Could not connect" : "Disconnected";
            string currentName = null;
            if (activeInvitation != null && (state == "starting" || state == "connected"))
                foreach (Workspace value in workspaces.Items) if (value.Identity == Invitation.Identity(activeInvitation)) currentName = value.Name;
            summary.Text = currentName == null ? message : currentName + Environment.NewLine + message;
            activity.Visible = state == "starting";
            link.Enabled = copy.Enabled = address.Enabled = state == "connected";
            disconnect.Enabled = state == "starting" || state == "connected";
            if (state != "connected") SetModelVisible(false);
            Text = "ComfyUI Notch - " + heading.Text;
            UpdateConnectButton();
        }

        private void UpdateConnectButton()
        {
            var selected = workspaces.SelectedItem as Workspace;
            bool different = selected != null && (activeInvitation == null || selected.Identity != Invitation.Identity(activeInvitation));
            reconnect.Text = different || activeInvitation == null ? "Connect" : "Reconnect";
            reconnect.Enabled = different || ((phaseState != "starting" && phaseState != "connected") && activeInvitation != null);
            reconnect.BackColor = reconnect.Enabled ? Color.FromArgb(48, 59, 199) : Color.FromArgb(239, 241, 246);
            reconnect.ForeColor = reconnect.Enabled ? Color.White : Color.FromArgb(120, 126, 140);
        }

        private void RefreshWorkspaces()
        {
            string selected = activeInvitation == null ? null : Invitation.Identity(activeInvitation);
            workspaces.Items.Clear();
            foreach (Workspace value in Workspace.Load(storageRoot)) workspaces.Items.Add(value);
            noWorkspaces.Visible = workspaces.Items.Count == 0;
            for (int i = 0; i < workspaces.Items.Count; i++) if (((Workspace)workspaces.Items[i]).Identity == selected) workspaces.SelectedIndex = i;
            if (workspaces.SelectedIndex < 0 && workspaces.Items.Count != 0) workspaces.SelectedIndex = 0;
            SetPhase(phaseState, phaseMessage);
        }

        private void ConnectSelected()
        {
            if (!reconnect.Enabled) return;
            var selected = workspaces.SelectedItem as Workspace;
            if (selected != null && activeInvitation != null && selected.Identity == Invitation.Identity(activeInvitation)) Connect(selected.Bookmark);
            else if (selected != null) Accept(selected.Bookmark, "");
            else if (activeInvitation != null) Connect(activeInvitation);
        }

        private void RemoveWorkspace()
        {
            var selected = workspaces.SelectedItem as Workspace;
            if (selected == null) return;
            bool current = activeInvitation != null && selected.Identity == Invitation.Identity(activeInvitation);
            if (MessageBox.Show(this, "Remove " + selected.Name + " from this computer?" + (current && process != null && !process.HasExited ? "\nThe current connection will close." : "") +
                "\nYou will need a new invitation to add it again.", "Remove saved workspace", MessageBoxButtons.OKCancel, MessageBoxIcon.Question) != DialogResult.OK) return;
            try
            {
                if (current) { Stop(); activeInvitation = null; live.Clear(); SetPhase("disconnected", "Workspace removed from this computer."); }
                selected.Remove(storageRoot);
                RefreshWorkspaces();
            }
            catch (IOException) { MessageBox.Show(this, "Windows could not remove the saved workspace. Close the connection and try again.", Text, MessageBoxButtons.OK, MessageBoxIcon.Warning); }
            catch (UnauthorizedAccessException) { MessageBox.Show(this, "Windows could not remove the saved workspace. Check this account's file permissions.", Text, MessageBoxButtons.OK, MessageBoxIcon.Warning); }
        }

        internal void Accept(string invitation, string session)
        {
            if (closing) return;
            Show(); WindowState = FormWindowState.Normal; Activate();
            if (String.IsNullOrEmpty(invitation)) return;
            if (process != null && !process.HasExited)
            {
                if (activeInvitation != null && Invitation.Identity(activeInvitation) == Invitation.Identity(invitation)) { live.Add(session); if (link.Enabled) OpenBrowser(); return; }
                if (MessageBox.Show(this, "Replace the current connection with this invitation?", Text,
                    MessageBoxButtons.OKCancel, MessageBoxIcon.Question) != DialogResult.OK) return;
            }
            Connect(invitation, session);
        }

        private void Connect(string invitation, string session = "")
        {
            try
            {
                Stop();
                live.Start(session, activeInvitation == null || Invitation.Identity(activeInvitation) != Invitation.Identity(invitation));
                activeInvitation = Invitation.Normalize(invitation);
                string script = Storage.ExtractLauncher();
                accessPath = Path.Combine(Storage.Root, "invitation-" + Guid.NewGuid().ToString("N"));
                stopPath = Path.Combine(Storage.Root, "stop-" + Guid.NewGuid().ToString("N"));
                sessionPath = Path.Combine(Storage.Root, "session-" + Guid.NewGuid().ToString("N"));
                File.WriteAllText(accessPath, activeInvitation, new UTF8Encoding(false));
                log.Clear(); SetModelVisible(false);
                SetPhase("starting", "Starting your workspace. Keep this window open while connected.");
                var info = new ProcessStartInfo(Program.PowerShell,
                    "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File " + Storage.Quote(script) +
                    " -AccessFile " + Storage.Quote(accessPath) + " -EnvFile " + Storage.Quote(Path.Combine(Storage.Root, ".env")) +
                    " -ConnectorMode -StopFile " + Storage.Quote(stopPath) + " -SessionDirectory " + Storage.Quote(sessionPath))
                { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
                job = new ChildJob();
                process = new Process { StartInfo = info, EnableRaisingEvents = true };
                process.OutputDataReceived += Output;
                process.ErrorDataReceived += Output;
                process.Exited += delegate(object sender, EventArgs args) { Post(delegate { if (!closing && Object.ReferenceEquals(sender, process)) { live.Ended(); SetPhase(live.State, live.Message); Append("Connection ended. Click Reconnect to try again."); } }); };
                process.Start(); job.Add(process);
                process.BeginOutputReadLine(); process.BeginErrorReadLine();
            }
            catch (Exception error) { Stop(); live.Set("error", ""); SetPhase("error", live.Message); Append("Could not connect: " + error.Message); }
        }

        private void Output(object sender, DataReceivedEventArgs args)
        {
            if (args.Data == null) return;
            Post(delegate
            {
                if (!Object.ReferenceEquals(sender, process)) return;
                if (args.Data == "HOSTED_COMFYUI_READY=" + LocalUrl)
                {
                    if (process.HasExited) return;
                    live.Set("connected", "Connected");
                    SetPhase("connected", "Your workspace is ready. Keep this window open while connected.");
                    DeleteAccessFile(); OpenBrowser();
                }
                else if (args.Data.StartsWith("HOSTED_COMFYUI_SAVED=", StringComparison.Ordinal))
                {
                    try
                    {
                        string bookmark = Invitation.SavedBookmark(args.Data.Substring("HOSTED_COMFYUI_SAVED=".Length));
                        if (activeInvitation != null && Invitation.Identity(activeInvitation) == Invitation.Identity(bookmark))
                        {
                            activeInvitation = bookmark;
                            live.Enrolled();
                            DeleteAccessFile();
                            RefreshWorkspaces();
                        }
                    }
                    catch (ArgumentException) { Append("The saved workspace could not be read."); }
                }
                else if (args.Data.StartsWith("HOSTED_COMFYUI_STORAGE=", StringComparison.Ordinal))
                {
                    ModelProgress value;
                    if (link.Enabled && ModelProgress.TryDecode(args.Data.Substring("HOSTED_COMFYUI_STORAGE=".Length), out value))
                    {
                        ShowModel(value);
                    }
                }
                else { live.Observe(args.Data); if (live.State != "connected") SetPhase(live.State, live.Message); Append(args.Data); }
            });
        }

        private void Post(Action action) { if (!closing && IsHandleCreated) { try { BeginInvoke(action); } catch (InvalidOperationException) { } } }
        internal void ShowModel(ModelProgress value)
        {
            modelStatus.Text = value.Text;
            SetModelVisible(value.Phase != "idle");
            modelProgress.Visible = value.Phase != "error";
            modelProgress.Style = value.Percent < 0 || value.Phase == "verifying" ? ProgressBarStyle.Marquee : ProgressBarStyle.Continuous;
            modelProgress.Value = Math.Max(0, value.Percent);
            live.SetModel(value);
        }

        private void SetModelVisible(bool visible)
        {
            if (modelVisible == visible) return;
            modelVisible = visible;
            int delta = visible ? modelPanel.Height : -modelPanel.Height;
            modelPanel.Visible = visible;
            if (visible) ClientSize = new Size(ClientSize.Width, ClientSize.Height + delta);
            MinimumSize = new Size(MinimumSize.Width, MinimumSize.Height + delta);
            if (!visible) ClientSize = new Size(ClientSize.Width, ClientSize.Height + delta);
        }
        internal void Append(string text)
        {
            if (text.Length > 4096) text = text.Substring(0, 4096) + "...";
            if (log.TextLength + text.Length > 24000) log.Text = log.Text.Substring(Math.Min(8000, log.TextLength));
            log.AppendText(text + Environment.NewLine);
        }
        private void OpenBrowser() { try { Process.Start(new ProcessStartInfo(LocalUrl) { UseShellExecute = true }); } catch { Append("Open " + LocalUrl + " in your browser."); } }
        private void DeleteAccessFile() { if (accessPath != null && File.Exists(accessPath)) File.Delete(accessPath); accessPath = null; }
        private void Stop()
        {
            live.Ended();
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
                    "ComfyUI Notch Connector", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
            stopPath = null;
            sessionPath = null;
        }
        protected override void OnFormClosing(FormClosingEventArgs args) { closing = true; heartbeat.Stop(); heartbeat.Dispose(); Stop(); base.OnFormClosing(args); }
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
                        throw new InvalidOperationException("Close ComfyUI Notch Connector before updating it.");
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
                        throw new InvalidOperationException("Close ComfyUI Notch Connector before uninstalling it.");
                    }
                    Installer.Remove(); return;
                }
                if (args.Length > 1) throw new ArgumentException("Open one invitation link at a time.");
                string session = "";
                string invitation = args.Length == 1 ? Invitation.DecodeUri(args[0], out session) : "";
                bool created;
                using (var mutex = new Mutex(true, @"Local\" + name, out created))
                {
                    if (!created)
                    {
                        if (args.Length == 0 && !Installer.IsPackaged() &&
                            !String.Equals(Application.ExecutablePath, Installer.ExePath, StringComparison.OrdinalIgnoreCase))
                        {
                            MessageBox.Show("Close ComfyUI Notch Connector, then reopen this installer to update it.",
                                "ComfyUI Notch Connector", MessageBoxButtons.OK, MessageBoxIcon.Information);
                            return;
                        }
                        using (var client = new NamedPipeClientStream(".", name, PipeDirection.Out))
                        {
                            client.Connect(5000);
                            using (var writer = new StreamWriter(client)) writer.WriteLine(session + "\t" + invitation);
                        }
                        return;
                    }
                    if (!Installer.EnsureInstalled(invitation.Length != 0)) return;
                    var form = new ConnectorForm();
                    var thread = new Thread(delegate() { Listen(name, form); });
                    thread.IsBackground = true;
                    form.Shown += delegate { thread.Start(); form.Accept(invitation, session); };
                    Application.Run(form);
                    mutex.ReleaseMutex();
                }
            }
            catch (Exception error) { MessageBox.Show(error.Message, "ComfyUI Notch Connector", MessageBoxButtons.OK, MessageBoxIcon.Error); }
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
                            while (message.Length <= 15100 && (value = reader.Read()) >= 0 && value != '\n') message.Append((char)value);
                            string[] parts = message.ToString().TrimEnd('\r').Split('\t');
                            if (parts.Length != 2 || (parts[0].Length != 0 && !LiveStatus.ValidSession(parts[0]))) continue;
                            string invitation = parts[1];
                            if (invitation.Length != 0) invitation = Invitation.Normalize(invitation);
                            form.BeginInvoke(new Action(delegate { form.Accept(invitation, parts[0]); }));
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

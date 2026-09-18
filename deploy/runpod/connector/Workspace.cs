using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Web.Script.Serialization;

namespace HostedComfyUI
{
    internal sealed class Workspace
    {
        internal string Name, Identity, Bookmark, Id;
        public override string ToString() { return Name; }

        internal static string Hash(string identity)
        {
            using (var hash = SHA256.Create()) return BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(identity))).Replace("-", "").ToLowerInvariant();
        }

        internal static Workspace Parse(string json)
        {
            var value = new JavaScriptSerializer { MaxJsonLength = 4096 }.DeserializeObject(json) as Dictionary<string, object>;
            object version, gateway, invite, name, device, local;
            if (value == null || !value.TryGetValue("version", out version) || !(version is int) ||
                !value.TryGetValue("name", out name) || !(name is string) || ((string)name).Length == 0 || ((string)name).Length > 80 ||
                (string)name != ((string)name).Trim() || Regex.IsMatch((string)name, @"[\x00-\x1f\x7f]")) throw new ArgumentException("Invalid saved workspace.");
            string identity;
            Dictionary<string, object> bookmark;
            if ((int)version == 2)
            {
                if (value.Count != 5 || !value.TryGetValue("gateway", out gateway) || !(gateway is string) || !Invitation.ValidGateway((string)gateway) ||
                    !value.TryGetValue("invite_id", out invite) || !(invite is string) || !LiveStatus.ValidSession((string)invite) ||
                    !value.TryGetValue("device_id", out device) || !(device is string) || !LiveStatus.ValidSession((string)device)) throw new ArgumentException("Invalid saved workspace.");
                identity = gateway + "|" + invite;
                bookmark = new Dictionary<string, object> { { "version", 2 }, { "gateway", gateway }, { "invite_id", invite }, { "token", "" } };
            }
            else if ((int)version == 3)
            {
                if (value.Count != 3 || !value.TryGetValue("workspace_id", out local) || !(local is string) || !Regex.IsMatch((string)local, @"\A[0-9a-f]{64}\z")) throw new ArgumentException("Invalid saved workspace.");
                identity = "legacy|" + local;
                bookmark = new Dictionary<string, object> { { "version", 3 }, { "workspace_id", local } };
            }
            else throw new ArgumentException("Invalid saved workspace.");
            return new Workspace { Name = (string)name, Identity = identity, Id = Hash(identity),
                Bookmark = Convert.ToBase64String(Encoding.UTF8.GetBytes(new JavaScriptSerializer().Serialize(bookmark))) };
        }

        internal static Workspace SaveLegacy(string root, string encoded)
        {
            var value = Invitation.Fields(Invitation.Normalize(encoded));
            if ((int)value["version"] != 1) throw new ArgumentException("This invitation does not need importing.");
            string local = Hash("legacy|" + value["endpoint"] + "|" + value["key"]);
            string descriptor = new JavaScriptSerializer().Serialize(new Dictionary<string, object> {
                { "version", 3 }, { "workspace_id", local }, { "name", "ComfyUI Notch (" + value["endpoint"] + ")" } });
            Workspace item = Parse(descriptor);
            if (Directory.Exists(root) && Linked(root)) throw new IOException("The workspace folder must not be a link.");
            Storage.ProtectDirectory(root);
            string folder = Path.Combine(root, "workspaces");
            if (Directory.Exists(folder) && Linked(folder)) throw new IOException("The workspace folder must not be a link.");
            Storage.ProtectDirectory(folder);
            string path = Path.Combine(folder, item.Id + ".dat");
            if (File.Exists(path + ".lock") && Linked(path + ".lock")) throw new IOException("The workspace file must not be a link.");
            using (var guard = new FileStream(path + ".lock", FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None))
            {
                byte[] plain = Encoding.UTF8.GetBytes(new JavaScriptSerializer().Serialize(value));
                try { WriteFile(path, ProtectedData.Protect(plain, null, DataProtectionScope.CurrentUser)); }
                finally { Array.Clear(plain, 0, plain.Length); }
                WriteFile(Path.ChangeExtension(path, ".json"), Encoding.UTF8.GetBytes(descriptor));
                SaveLast(root, item.Bookmark);
            }
            return item;
        }

        internal static void ImportLegacy(string root)
        {
            foreach (string line in ReadLast(root))
            {
                Match match = Regex.Match(line, @"\A\s*HOSTED_COMFYUI_ACCESS\s*[:=]\s*(.+)\z");
                if (!match.Success) continue;
                string encoded = Invitation.Normalize(match.Groups[1].Value.Trim().Trim('\"', '\''));
                if (Invitation.Version(encoded) == 1) SaveLegacy(root, encoded);
                return;
            }
        }

        private static string[] ReadLast(string root)
        {
            string path = Path.Combine(root, ".env");
            if (!File.Exists(path)) return new string[0];
            if (Linked(path) || new FileInfo(path).Length > 131072) throw new IOException("The saved invitation file cannot be read.");
            return File.ReadAllLines(path);
        }

        private static void SaveLast(string root, string bookmark)
        {
            var lines = ReadLast(root).Where(line => !Regex.IsMatch(line, @"\A\s*HOSTED_COMFYUI_ACCESS\s*[:=]")).ToList();
            if (bookmark != null) lines.Add("HOSTED_COMFYUI_ACCESS=" + bookmark);
            WriteFile(Path.Combine(root, ".env"), Encoding.UTF8.GetBytes(String.Join(Environment.NewLine, lines) + Environment.NewLine));
        }

        private static void WriteFile(string path, byte[] bytes)
        {
            string temporary = path + "." + Guid.NewGuid().ToString("N");
            try
            {
                File.WriteAllBytes(temporary, bytes);
                if (File.Exists(path))
                {
                    if (Linked(path)) throw new IOException("The workspace file must not be a link.");
                    File.Replace(temporary, path, null);
                }
                else File.Move(temporary, path);
            }
            finally { if (File.Exists(temporary)) File.Delete(temporary); }
        }

        internal static List<Workspace> Load(string root)
        {
            var result = new List<Workspace>();
            string folder = Path.Combine(root, "workspaces");
            if (!Directory.Exists(folder) || Linked(folder)) return result;
            foreach (string path in Directory.EnumerateFiles(folder, "*.json").Take(256))
            {
                try
                {
                    if (new FileInfo(path).Length > 4096 || Linked(path)) continue;
                    Workspace value = Parse(File.ReadAllText(path));
                    string vault = Path.Combine(folder, value.Id + ".dat");
                    if (Path.GetFileNameWithoutExtension(path) == value.Id && File.Exists(vault) && !Linked(vault)) result.Add(value);
                }
                catch (IOException) { }
                catch (UnauthorizedAccessException) { }
                catch (ArgumentException) { }
                catch (InvalidOperationException) { }
            }
            return result.OrderBy(item => item.Name, StringComparer.CurrentCultureIgnoreCase).ToList();
        }

        internal void Remove(string root)
        {
            string folder = Path.Combine(root, "workspaces");
            string id = Hash(Identity);
            if (Linked(folder)) throw new IOException("The workspace folder must not be a link.");
            foreach (string extension in new[] { ".json", ".dat", ".dat.lock" })
            {
                string path = Path.Combine(folder, id + extension);
                if (File.Exists(path) && Linked(path)) throw new IOException("The workspace file must not be a link.");
            }
            using (var guard = new FileStream(Path.Combine(folder, id + ".dat.lock"), FileMode.OpenOrCreate, FileAccess.ReadWrite, FileShare.None))
            {
                File.Delete(Path.Combine(folder, id + ".dat"));
                File.Delete(Path.Combine(folder, id + ".json"));
                foreach (string line in ReadLast(root))
                {
                    Match match = Regex.Match(line, @"\A\s*HOSTED_COMFYUI_ACCESS\s*[:=]\s*(.+)\z");
                    if (!match.Success) continue;
                    try { if (Invitation.Identity(Invitation.Normalize(match.Groups[1].Value.Trim().Trim('\"', '\''))) == Identity) SaveLast(root, null); }
                    catch (ArgumentException) { }
                    break;
                }
            }
        }

        private static bool Linked(string path) { return (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0; }
    }
}

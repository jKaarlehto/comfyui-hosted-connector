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
            object version, gateway, invite, name, device;
            if (value == null || value.Count != 5 || !value.TryGetValue("version", out version) || !(version is int) || (int)version != 2 ||
                !value.TryGetValue("gateway", out gateway) || !(gateway is string) || !Invitation.ValidGateway((string)gateway) ||
                !value.TryGetValue("invite_id", out invite) || !(invite is string) || !LiveStatus.ValidSession((string)invite) ||
                !value.TryGetValue("device_id", out device) || !(device is string) || !LiveStatus.ValidSession((string)device) ||
                !value.TryGetValue("name", out name) || !(name is string) || ((string)name).Length == 0 || ((string)name).Length > 80 ||
                (string)name != ((string)name).Trim() || Regex.IsMatch((string)name, @"[\x00-\x1f\x7f]")) throw new ArgumentException("Invalid saved workspace.");
            string identity = gateway + "|" + invite;
            var bookmark = new Dictionary<string, object> { { "version", 2 }, { "gateway", gateway }, { "invite_id", invite }, { "token", "" } };
            return new Workspace { Name = (string)name, Identity = identity, Id = Hash(identity),
                Bookmark = Convert.ToBase64String(Encoding.UTF8.GetBytes(new JavaScriptSerializer().Serialize(bookmark))) };
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
            }
        }

        private static bool Linked(string path) { return (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0; }
    }
}

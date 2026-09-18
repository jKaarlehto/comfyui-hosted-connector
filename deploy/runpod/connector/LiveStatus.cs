using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;
using System.Web.Script.Serialization;

namespace HostedComfyUI
{
    internal sealed class LiveStatus
    {
        internal const string Address = "http://127.0.0.1:18188";
        private readonly string root;
        private readonly List<string> sessions = new List<string>();
        private string state = "starting", message = "Starting hosted ComfyUI";
        private ModelProgress model;
        private static readonly string[] Steps = {
            "Checking for updates", "Downloading Notch plugin", "Starting connection services", "Updating ComfyUI",
            "Installing ComfyUI dependencies", "Preparing saved files", "Starting ComfyUI", "Starting hosted ComfyUI",
            "Waiting for GPU capacity; retrying once a minute", "Waiting for GPU capacity within the configured price limit",
            "That GPU is unavailable; checking the next configured GPU", "Preparing a replacement GPU and restoring persistent files",
            "Starting the replacement GPU", "Waiting for SSH", "Waiting for the server's startup status",
            "Runpod is taking longer to respond; checking again shortly", "The starter is temporarily unavailable; retrying...",
            "Applying updated startup settings before starting ComfyUI", "Checking ComfyUI readiness on the next connection request",
            "Another recovery request is being reconciled", "Checking whether Runpod accepted the replacement",
            "Reconciling the replacement with Runpod before retrying", "Stopping a duplicate recovery allocation",
            "Retiring a duplicate recovery allocation", "The replacement GPU became unavailable; trying another",
            "Stopping the original Pod before completing recovery",
            "Checking Windows SSH client", "Loading your tester invitation", "Waking hosted ComfyUI",
            "Opening the local connection", "Waiting for ComfyUI and the plugin to finish loading"
        };
        private static readonly string[] Failures = {
            "Could not check for updates; the owner must check GitHub access",
            "Could not download the Notch plugin; the owner must check GitHub access and the deploy key",
            "Could not start connection services; contact the owner", "Could not update ComfyUI; the owner must check GitHub access",
            "Could not install ComfyUI dependencies; contact the owner", "Could not prepare persistent files; the owner must check storage",
            "ComfyUI failed to start; the owner must check the startup log",
            "This invitation has expired or was revoked. Ask the owner for a new code.",
            "Windows OpenSSH Client is missing. Install it under Settings > Optional features, then reconnect.",
            "The server rejected your tunnel key. Ask the owner to renew your invitation."
        };

        internal LiveStatus(string directory) { root = directory; }
        internal static bool ValidSession(string token) { return token != null && Regex.IsMatch(token, @"\A[0-9a-f]{32}\z"); }

        internal void Add(string token)
        {
            if (!ValidSession(token) || sessions.Contains(token)) return;
            if (sessions.Count == 16) sessions.RemoveAt(0);
            sessions.Add(token);
            Publish();
        }

        internal void Clear() { Set("disconnected", "Connection closed"); sessions.Clear(); }
        internal void Start(string token, bool replace)
        {
            if (replace) Clear();
            Set("starting", "Starting hosted ComfyUI");
            Add(token);
        }
        internal void Set(string nextState, string nextMessage)
        {
            state = nextState; message = SafeMessage(state, nextMessage);
            if (state != "connected") model = null;
            Publish();
        }
        internal void SetModel(ModelProgress value) { model = value.Phase == "idle" ? null : value; Publish(); }
        internal void Ended() { if (state != "error") Set("disconnected", "Connection ended. Reconnect to try again."); }
        internal void Heartbeat() { if (state == "starting" || state == "connected") Publish(); }

        internal void Observe(string line)
        {
            if (line.StartsWith("Connection failed: ", StringComparison.Ordinal))
            {
                Set("error", line.Substring("Connection failed: ".Length));
                return;
            }
            if (state != "starting") return;
            string text = Regex.Replace(line, @"\A\[\d{2}:\d{2}:\d{2}\] (?:[1-5]/5 )?", "");
            if (text == "Waking hosted ComfyUI (this may take a few minutes)") text = "Waking hosted ComfyUI";
            if (Array.IndexOf(Steps, text) >= 0 && text != message) Set("starting", text);
        }

        private static string SafeMessage(string state, string text)
        {
            if (state == "connected") return "Connected";
            if (state == "disconnected") return "Connection ended. Reconnect to try again.";
            if (state == "error") return Array.IndexOf(Failures, text) >= 0 ? text : "Could not connect. Reconnect or ask the owner for help.";
            return Array.IndexOf(Steps, text) >= 0 ? text : "Starting hosted ComfyUI";
        }

        internal void Publish()
        {
            if (sessions.Count == 0) return;
            try
            {
                string folder = Path.Combine(root, "page-sessions");
                Storage.ProtectDirectory(root);
                Storage.ProtectDirectory(folder);
                using (Process process = Process.GetCurrentProcess())
                {
                    var value = new Dictionary<string, object> {
                        { "state", state }, { "message", message }, { "address", state == "connected" ? Address : "" },
                        { "process_id", process.Id }, { "process_start", process.StartTime.ToUniversalTime().Ticks },
                        { "updated_at", DateTime.UtcNow.Ticks }
                    };
                    if (model != null && state == "connected") value["model"] = ModelFields(model);
                    byte[] data = Encoding.UTF8.GetBytes(new JavaScriptSerializer().Serialize(value));
                    foreach (string token in sessions) Write(folder, token, data);
                }
                string[] files = Directory.GetFiles(folder, "*.json").Where(path => ValidSession(Path.GetFileNameWithoutExtension(path)))
                    .OrderByDescending(File.GetLastWriteTimeUtc).ToArray();
                for (int i = 0; i < files.Length; i++)
                    if (i >= 64 || File.GetLastWriteTimeUtc(files[i]) < DateTime.UtcNow.AddMinutes(-30)) File.Delete(files[i]);
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }

        private static Dictionary<string, object> ModelFields(ModelProgress value)
        {
            var result = new Dictionary<string, object> { { "phase", value.Phase }, { "filename", value.Filename },
                { "completed_bytes", value.Completed }, { "total_bytes", value.Total } };
            if (value.Phase == "error") result["error"] = "Model download failed. See ComfyUI for details.";
            return result;
        }

        private static void Write(string folder, string token, byte[] data)
        {
            string path = Path.Combine(folder, token + ".json"), temporary = path + "." + Guid.NewGuid().ToString("N");
            try
            {
                File.WriteAllBytes(temporary, data);
                if (File.Exists(path)) File.Replace(temporary, path, null);
                else File.Move(temporary, path);
            }
            finally { if (File.Exists(temporary)) File.Delete(temporary); }
        }

        internal static Dictionary<string, object> Read(string root, string token)
        {
            if (!ValidSession(token)) return null;
            try
            {
                string path = Path.Combine(root, "page-sessions", token + ".json");
                string data;
                using (var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete))
                {
                    if (stream.Length > 16384) return null;
                    using (var reader = new StreamReader(stream, Encoding.UTF8)) data = reader.ReadToEnd();
                }
                var fields = new JavaScriptSerializer { MaxJsonLength = 16384 }.DeserializeObject(data) as Dictionary<string, object>;
                object updated, pid, started, rawState, rawMessage, rawModel;
                if (fields == null || !fields.TryGetValue("updated_at", out updated) || !(updated is long) ||
                    !fields.TryGetValue("process_id", out pid) || !(pid is int) ||
                    !fields.TryGetValue("process_start", out started) || !(started is long) ||
                    !fields.TryGetValue("state", out rawState) || !(rawState is string) ||
                    !fields.TryGetValue("message", out rawMessage) || !(rawMessage is string)) return null;
                long age = DateTime.UtcNow.Ticks - (long)updated;
                if (age < -TimeSpan.TicksPerMinute || age > TimeSpan.TicksPerMinute * 30) return null;
                string state = (string)rawState;
                if (state != "starting" && state != "connected" && state != "error" && state != "disconnected") return null;
                if ((state == "starting" || state == "connected") &&
                    (age > TimeSpan.TicksPerSecond * 15 || !Alive((int)pid, (long)started))) state = "disconnected";
                var result = new Dictionary<string, object> { { "state", state }, { "message", SafeMessage(state, (string)rawMessage) },
                    { "address", state == "connected" ? Address : "" } };
                if (state == "connected" && fields.TryGetValue("model", out rawModel))
                {
                    ModelProgress progress;
                    string encoded = Convert.ToBase64String(Encoding.UTF8.GetBytes(new JavaScriptSerializer().Serialize(rawModel)));
                    if (ModelProgress.TryDecode(encoded, out progress) && progress.Phase != "idle") result["model"] = ModelFields(progress);
                }
                return result;
            }
            catch (IOException) { return null; }
            catch (UnauthorizedAccessException) { return null; }
            catch (ArgumentException) { return null; }
            catch (InvalidOperationException) { return null; }
        }

        private static bool Alive(int pid, long started)
        {
            try { using (Process process = Process.GetProcessById(pid)) return !process.HasExited && process.StartTime.ToUniversalTime().Ticks == started; }
            catch (ArgumentException) { return false; }
            catch (InvalidOperationException) { return false; }
            catch (System.ComponentModel.Win32Exception) { return false; }
        }
    }
}

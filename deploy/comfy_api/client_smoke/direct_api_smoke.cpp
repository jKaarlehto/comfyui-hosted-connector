// Direct C++ v2 client over real HTTP, with no local bridge or WebSocket.
#include "comfy_extension_client/comfy_publish.hpp"
#include <curl/curl.h>
#include <chrono>
#include <cctype>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <thread>
using namespace ComfyExtensionClient;

class ApiHttp : public ComfyApiHttpTransport
{
    struct Sink { std::string* bytes; size_t cap; };
    static size_t Write(char* data, size_t size, size_t count, void* target)
    {
        Sink& sink = *static_cast<Sink*>(target);
        size_t length = size * count;
        if (length > sink.cap - sink.bytes->size()) return 0;
        sink.bytes->append(data, length); return length;
    }
    static size_t Header(char* data, size_t size, size_t count, void* target)
    {
        std::string line(data, size * count);
        if (line.size() >= 9)
        {
            std::string key = line.substr(0, 9);
            for (size_t i = 0; i < key.size(); ++i) key[i] = static_cast<char>(std::tolower(key[i]));
            if (key == "location:")
            {
                size_t start = line.find_first_not_of(" \t", 9);
                size_t end = line.find_last_not_of("\r\n \t");
                static_cast<ComfyApiHttpResponse*>(target)->location =
                    start == std::string::npos ? "" : line.substr(start, end - start + 1);
            }
        }
        return size * count;
    }
public:
    bool Send(const ComfyApiHttpRequest& request, ComfyApiHttpResponse& response, std::string& error) override
    {
        CURL* curl = curl_easy_init();
        if (!curl) return false;
        Sink sink = { &response.body, request.max_response_bytes };
        curl_easy_setopt(curl, CURLOPT_URL, request.url.c_str());
        curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, request.method.c_str());
        curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 0L);
        curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 1L);
        curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 2L);
        curl_easy_setopt(curl, CURLOPT_TIMEOUT, 120L);
        curl_easy_setopt(curl, CURLOPT_NOPROXY, "127.0.0.1,localhost");
        curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, Write);
        curl_easy_setopt(curl, CURLOPT_WRITEDATA, &sink);
        curl_easy_setopt(curl, CURLOPT_HEADERFUNCTION, Header);
        curl_easy_setopt(curl, CURLOPT_HEADERDATA, &response);
        curl_slist* headers = NULL;
        for (std::map<std::string, std::string>::const_iterator i = request.headers.begin(); i != request.headers.end(); ++i)
            headers = curl_slist_append(headers, (i->first + ": " + i->second).c_str());
        curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
        if (request.method == "POST")
        {
            curl_easy_setopt(curl, CURLOPT_POSTFIELDS, request.body.data());
            curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE_LARGE, static_cast<curl_off_t>(request.body.size()));
        }
        CURLcode result = curl_easy_perform(curl);
        long status = 0;
        curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &status);
        response.status_code = static_cast<int>(status);
        curl_slist_free_all(headers); curl_easy_cleanup(curl);
        if (result != CURLE_OK) error = "HTTP request failed";
        return result == CURLE_OK;
    }
};

static void Save(const std::string& path, const std::string& value)
{
    std::ofstream file(path.c_str(), std::ios::binary | std::ios::trunc);
    file << value; file.close();
    if (!file) throw std::runtime_error("Could not persist smoke-test state");
}
static std::string Read(const std::string& path)
{
    std::ifstream file(path.c_str(), std::ios::binary);
    return std::string(std::istreambuf_iterator<char>(file), std::istreambuf_iterator<char>());
}

int main(int argc, char** argv)
{
    try
    {
        if (argc != 4) throw std::runtime_error("usage: direct_api_smoke <endpoint> <input.png> <state-file-prefix>; run only one instance per state prefix");
        curl_global_init(CURL_GLOBAL_DEFAULT);
        ApiHttp http;
        ComfyApiOptions options;
        options.endpoint = argv[1]; options.transport = &http;
        options.allow_loopback_for_development = true;
        const char* key = std::getenv("COMFY_API_KEY");
        if (key) options.api_key = key;
        ComfyApiClient client(options);
        if (!client.ValidateConfiguration()) throw std::runtime_error(client.ValidateConfiguration().ErrorMessage());
        std::string prefix = argv[3];
        // A consumer begins with only the native definition URL and credential.
        // DFX persistence is supplied by the SDK snapshot serializer, not by an
        // application-specific file writer in this development smoke.
        ComfyWorkflowSnapshot snapshot;
        snapshot.name = "Native metadata smoke";
        snapshot.workflow_json = "{\"1\":{\"class_type\":\"EmptyImage\",\"inputs\":{\"width\":16,\"height\":16,\"batch_size\":1,\"color\":0}},\"2\":{\"class_type\":\"SaveImage\",\"inputs\":{\"images\":[\"1\",0],\"filename_prefix\":\"metadata-smoke\"}}}";
        snapshot.bindings_json = "{\"inputs\":{},\"outputs\":[{\"name\":\"image\",\"type\":\"IMAGE\",\"node_id\":\"2\"}]}";
        snapshot.reference.deployment_url = options.endpoint;
        snapshot.reference.workflow_version = "native-metadata-smoke-v1";
        std::string workflowURL = Read(prefix + ".publication-url");
        if (workflowURL.empty())
        {
            Result<ComfyWorkflowReference> published = client.PublishWorkflowSnapshot(snapshot);
            if (!published) throw std::runtime_error(published.ErrorMessage());
            Result<std::string> url = published.Value().PublicationURL(true);
            if (!url) throw std::runtime_error(url.ErrorMessage());
            workflowURL = url.Value(); Save(prefix + ".publication-url", workflowURL);
        }
        Result<ComfyWorkflowReference> entered = ComfyWorkflowReference::FromPublicationURL(workflowURL, true);
        if (!entered || entered.Value().deployment_url != options.endpoint)
            throw std::runtime_error("Saved publication URL differs from the selected endpoint");
        // The consuming client gets its origin from the entered URL. It has no
        // graph until LoadWorkflowSnapshot retrieves the publisher's asset.
        ComfyApiOptions consumerOptions = options;
        consumerOptions.endpoint = entered.Value().deployment_url;
        ComfyApiClient consumer(consumerOptions);
        Result<ComfyWorkflowSnapshot> fetched = consumer.LoadWorkflowSnapshot(workflowURL);
        if (!fetched || fetched.Value().workflow_json != snapshot.workflow_json)
            throw std::runtime_error(fetched ? "Loaded workflow differs from published graph" : fetched.ErrorMessage());
        Result<std::string> persisted = fetched.Value().Serialize(true);
        if (!persisted || (key && persisted.Value().find(key) != std::string::npos))
            throw std::runtime_error("Credential-free snapshot persistence failed");
        Save(prefix + ".workflow.json", persisted.Value());
        std::cout << "Native JSON asset publication, URL-only definition fetch and credential-free persistence passed; retention: "
            << (fetched.Value().reference.definition_expires_at.empty() ? "provider reports no expiry" : fetched.Value().reference.definition_expires_at) << "\n";
        std::string jobId = Read(prefix + ".job");
        if (jobId.empty())
        {
            if (!Read(prefix + ".intent").empty())
                throw std::runtime_error("An earlier submission may exist. Recover its job ID; refusing to resubmit.");
            std::string image = Read(argv[2]);
            Result<std::string> asset = client.UploadImage(std::vector<uint8_t>(image.begin(), image.end()), "input.png", "image/png");
            if (!asset) throw std::runtime_error(asset.ErrorMessage());
            std::string graph = "{\"1\":{\"class_type\":\"LoadImage\",\"inputs\":{\"image\":{\"__type\":\"core/ASSET\",\"info\":{\"id\":\"" +
                asset.Value() + "\"}}}},\"2\":{\"class_type\":\"SaveImage\",\"inputs\":{\"images\":[\"1\",0],\"filename_prefix\":\"direct-api-smoke\"}}}";
            std::string intent = "notch-smoke-" + std::to_string(std::chrono::high_resolution_clock::now().time_since_epoch().count());
            Save(prefix + ".intent", intent);
            Result<ComfyApiJob> submitted = client.Submit(graph, intent);
            if (!submitted) throw std::runtime_error(submitted.ErrorMessage());
            jobId = submitted.Value().id; Save(prefix + ".job", jobId);
        }
        for (int attempt = 0; attempt < 120; ++attempt)
        {
            Result<ComfyApiJob> polled = client.GetJob(jobId);
            if (!polled) throw std::runtime_error(polled.ErrorMessage());
            if (polled.Value().Terminal())
            {
                if (polled.Value().status != "succeeded" || !polled.Value().snapshot_error.empty() || polled.Value().outputs.empty())
                    throw std::runtime_error("Job did not produce a valid successful output");
                Result<std::vector<uint8_t> > bytes = client.DownloadAsset(polled.Value().outputs[0].asset_id);
                if (!bytes) throw std::runtime_error(bytes.ErrorMessage());
                std::string png(bytes.Value().begin(), bytes.Value().end());
                if (png.size() < 8 || png.substr(0, 8) != std::string("\x89PNG\r\n\x1a\n", 8))
                    throw std::runtime_error("Expected PNG output");
                Save(prefix + ".png", png);
                std::cout << "Direct v2 C++ upload, generation, polling and download passed; saved job resumes without submission.\n";
                return 0;
            }
            std::this_thread::sleep_for(std::chrono::milliseconds(500));
        }
        throw std::runtime_error("Job still pending; run again with the same state prefix to resume");
    }
    catch (const std::exception& error) { std::cerr << error.what() << '\n'; return 1; }
}

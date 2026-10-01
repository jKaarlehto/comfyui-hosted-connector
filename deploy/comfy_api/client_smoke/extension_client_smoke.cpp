// Actual extension Client over local HTTP. Python relays its WS transport to
// the bridge so this test exercises handshake, auto-update bootstrap, contract,
// multipart raw image upload, output resolution, download and delivery ACKs.
#include "comfy_extension_client/client.hpp"
#include <curl/curl.h>
#include <iostream>
#include <stdexcept>
#include <string>

using namespace ComfyExtensionClient;

static void Require(bool value, const std::string& message)
{
    if (!value) throw std::runtime_error(message);
}

class Http : public HttpTransport
{
    std::string base;
    static size_t Write(char* ptr, size_t size, size_t count, void* target)
    {
        static_cast<std::string*>(target)->append(ptr, size * count);
        return size * count;
    }
public:
    explicit Http(const std::string& url) : base(url) {}
    bool Send(const HttpRequest& request, HttpResponse& response, std::string& error) override
    {
        CURL* handle = curl_easy_init();
        if (!handle) { error = "curl init failed"; return false; }
        std::string url = base + request.path;
        curl_easy_setopt(handle, CURLOPT_URL, url.c_str());
        curl_easy_setopt(handle, CURLOPT_CUSTOMREQUEST, request.method.c_str());
        curl_easy_setopt(handle, CURLOPT_TIMEOUT, 10L);
        curl_easy_setopt(handle, CURLOPT_NOPROXY, "127.0.0.1,localhost");
        curl_easy_setopt(handle, CURLOPT_WRITEFUNCTION, Write);
        curl_easy_setopt(handle, CURLOPT_WRITEDATA, &response.body);
        curl_slist* headers = nullptr;
        if (!request.content_type.empty())
        {
            headers = curl_slist_append(headers, ("Content-Type: " + request.content_type).c_str());
            curl_easy_setopt(handle, CURLOPT_HTTPHEADER, headers);
        }
        if (request.method == "POST")
        {
            curl_easy_setopt(handle, CURLOPT_POSTFIELDS, request.body.data());
            curl_easy_setopt(handle, CURLOPT_POSTFIELDSIZE, static_cast<long>(request.body.size()));
        }
        CURLcode result = curl_easy_perform(handle);
        long status = 0;
        curl_easy_getinfo(handle, CURLINFO_RESPONSE_CODE, &status);
        response.status_code = static_cast<int>(status);
        if (result != CURLE_OK) error = curl_easy_strerror(result);
        curl_slist_free_all(headers);
        curl_easy_cleanup(handle);
        return result == CURLE_OK;
    }
};

class Socket : public WebSocketTransport
{
public:
    bool SendText(const std::string& message, std::string&) override
    {
        std::string singleLine;
        for (char c : message) if (c != '\n' && c != '\r') singleLine += c;
        std::cout << "{\"send\":" << singleLine << "}" << std::endl;
        return true;
    }
};

static void Download(Http& transport, const OutputReady& output)
{
    HttpRequest request;
    request.method = "GET";
    request.path = output.http.url;
    HttpResponse response;
    std::string error;
    Require(transport.Send(request, response, error), error);
    Require(response.status_code == 200 && response.body.size() > 8 &&
            response.body.substr(0, 8) == std::string("\x89PNG\r\n\x1a\n", 8), "output was not a PNG");
}

int main(int argc, char** argv)
{
    try
    {
        Require(argc == 2, "usage: extension_client_smoke <loopback bridge URL>");
        curl_global_init(CURL_GLOBAL_DEFAULT);
        Http http(argv[1]);
        Socket socket;
        ClientOptions options;
        options.client_id = "cpp-client";
        options.http_transport = &http;
        options.websocket_transport = &socket;
        options.auto_update_plugin = true;
        Client client(options);
        Require(static_cast<bool>(client.OnWebSocketConnected()), "connect failed");
        std::string line;
        Require(static_cast<bool>(std::getline(std::cin, line)), "missing feature flags");
        Require(static_cast<bool>(client.HandleWebSocketText(line)), "invalid bridge feature flags");
        auto paths = client.GetServerWorkflows();
        Require(static_cast<bool>(paths) && paths.Value().size() == 1, "workflow list failed");
        auto workflow = client.GetServerWorkflow(paths.Value()[0]);
        Require(static_cast<bool>(workflow), "workflow download failed");
        auto contract = client.ParseWorkflow(workflow.Value());
        Require(static_cast<bool>(contract), contract ? "" : contract.GetError().message);
        Require(contract.Value().input_declarations.size() == 2 && contract.Value().outputs.size() == 1,
                "unexpected published contract");
        GenerateRequest request;
        request.workflow_json = workflow.Value();
        request.execute = true;
        request.inputs.push_back(InputValue::Integer("width", 16));
        auto image = InputValue::Binary("image", {255, 0, 0, 255}, "frame.raw", "IMAGE");
        image.raw_buffer.enabled = true;
        image.raw_buffer.width = 1;
        image.raw_buffer.height = 1;
        image.raw_buffer.format = "rgba";
        image.raw_buffer.stride = 4;
        request.inputs.push_back(image);
        OutputRequest output = OutputRequest::Http("image", "png");
        output.workflow_output_id = contract.Value().outputs[0].name;
        output.consumer_id = "cpp-beauty";
        request.AddOutput(output);
        auto job = client.Generate(request);
        Require(static_cast<bool>(job), job ? "" : job.GetError().message);
        Require(job.Value().queued, "job was not queued");
        std::cout << "{\"queued\":true}" << std::endl;
        bool delivered = false, completed = false;
        while (std::getline(std::cin, line))
        {
            auto event = client.HandleWebSocketText(line);
            Require(static_cast<bool>(event), event ? "" : event.GetError().message);
            for (const auto& result : event.Value().outputs)
            {
                Require(result.transport == OutputTransport::Http, "bridge advertised a non-HTTP result");
                Download(http, result);
                delivered = true;
            }
            if (event.Value().receipt)
                Require(static_cast<bool>(client.ConfirmDelivery(event.Value().receipt)), "delivery ACK failed");
            if (event.Value().kind == EventKind::ExecutionTerminal)
            {
                Require(event.Value().terminal_status == "success", "generation failed");
                completed = true;
                break;
            }
        }
        Require(delivered && completed, "output/terminal delivery was incomplete");
        auto latest = client.GetLatestOutputs();
        Require(static_cast<bool>(latest) && !latest.Value().empty(), "latest output discovery failed");
        auto resolved = client.ResolveOutput(job.Value().prompt_id, output.workflow_output_id, "cpp-resolved", OutputTransport::Http);
        Require(static_cast<bool>(resolved), "output resolution failed");
        Download(http, resolved.Value());
        std::cout << "{\"complete\":true}" << std::endl;
        return 0;
    }
    catch (const std::exception& error)
    {
        std::cerr << error.what() << std::endl;
        return 1;
    }
}

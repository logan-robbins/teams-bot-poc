# Teams Meeting Real-time Audio → Transcription → Python Agent (POC Guide)
## Validated + strengthened for 2025/2026 (build-first, internal-only)

**Last validated:** 2026-01-28 (America/Los_Angeles)
**Objective:** *Stream the audio from a Microsoft Teams meeting in real time → transcribe it → stream transcript events to a Python agent framework.*
**Non-goals:** Store publishing, commercial Teams Store submission, scalability, governance, hardening, or production readiness.

---

## What changed vs your original document (important corrections)

This guide is based on your original draft (attached in this workspace) and corrects/extends it where Microsoft’s official documentation requires different behavior.

1. **Media (TCP) requires your own DNS + a cert matching that DNS.**  
   Your draft suggests creating a self-signed cert for `*.ngrok.io` / `*.ngrok-free.app`. Microsoft’s official “develop calling/meeting bots locally” guidance requires:
   - **DNS CNAME(s)** you control pointing to ngrok’s fixed TCP endpoints (`0.tcp.ngrok.io`, `1.tcp.ngrok.io`, …)
   - An **SSL certificate** issued to a **wildcard domain** you control (for example `*.bot.contoso.com`) and **the cert must match the bot’s public media URL**.  
   → This is mandatory for an app-hosted media bot’s TCP media connection.  
   **Source:** S2.

2. **Calling/meeting bots require manifest flags (`supportsCalling`, optionally `supportsVideo`).**  
   Your draft skipped the Teams app manifest settings that enable calling/meeting participation.  
   **Source:** S1.

3. **For app-hosted media calls, you must configure `MediaPlatformSettings` correctly** (internal port, public port, service FQDN, certificate thumbprint).  
   **Source:** S2, S14.

4. **For app-hosted media, you must use the .NET media library + Windows hosting.**  
   Application-hosted media bots require the `Microsoft.Graph.Communications.Calls.Media` library and must run in a Windows environment; bots can’t be deployed as an Azure Web App.  
   **Source:** S3, S4.

5. **If you *persist* media or data derived from it (including transcripts), Microsoft Graph requires `updateRecordingStatus`** (and that API is tied to Teams policy-based recording).  
   This guide focuses on the mechanics to get a live stream + transcript into your Python agent.  
   If you later store transcripts/media, you must align with the Graph restriction.  
   **Source:** S8, S9, S1.

---

## Sources index (referenced by S# throughout)

> These are the authoritative sources used to validate every step (prioritizing Microsoft Learn + official Microsoft repos/docs).

- **S1** – Register calls and meetings bot for Teams (manifest + permissions):  
  https://learn.microsoft.com/en-us/microsoftteams/platform/bots/calls-and-meetings/registering-calling-bot
- **S2** – Develop calling and online meeting bots on your local PC (ngrok TCP + DNS + cert rules):  
  https://learn.microsoft.com/en-us/microsoftteams/platform/bots/calls-and-meetings/debugging-local-testing-calling-meeting-bots
- **S3** – Calls and online meetings bots overview (real-time media platform + concepts):  
  https://learn.microsoft.com/en-us/microsoftteams/platform/bots/calls-and-meetings/calls-meetings-bots-overview
- **S4** – Requirements and considerations for application-hosted media bots (Windows-only, cannot host on Azure Web App, etc.):  
  https://learn.microsoft.com/en-us/microsoftteams/platform/bots/calls-and-meetings/requirements-considerations-application-hosted-media-bots
- **S5** – Real-time media concepts for bots (20 ms audio frames, 50 fps, audio format):  
  https://learn.microsoft.com/en-us/microsoftteams/platform/bots/calls-and-meetings/real-time-media-concepts
- **S6** – Microsoft Graph create call (join scheduled meeting + `appHostedMediaConfig` example):  
  https://learn.microsoft.com/en-us/graph/api/application-post-calls?view=graph-rest-1.0
- **S7** – Microsoft Graph call resource type (call model: chatInfo/meetingInfo/mediaConfig/etc.):  
  https://learn.microsoft.com/en-us/graph/api/resources/call?view=graph-rest-1.0
- **S8** – Choose a media hosting option (Media Access API + recording restriction / `updateRecordingStatus` requirement):  
  https://learn.microsoft.com/en-us/graph/cloud-communications-media
- **S9** – Microsoft Graph `call: updateRecordingStatus` (policy-based recording):  
  https://learn.microsoft.com/en-us/graph/api/call-updaterecordingstatus?view=graph-rest-1.0
- **S10** – Graph comms bot media SDK docs index (concepts + terminology):  
  https://microsoftgraph.github.io/microsoft-graph-comms-samples/docs/bot_media/index.html
- **S11** – Graph comms samples “Testing” (ngrok + DNS + cert + MediaPlatformSettings examples):  
  https://microsoftgraph.github.io/microsoft-graph-comms-samples/docs/articles/Testing.html
- **S12** – Graph comms samples “Setting up application hosted media” (sample-specific guidance):  
  https://microsoftgraph.github.io/microsoft-graph-comms-samples/docs/articles/Setting_up_AppHostedMedia.html
- **S13** – Get started with the cloud communications API (Graph overview + prerequisites):  
  https://learn.microsoft.com/en-us/graph/cloud-communications-get-started
- **S14** – Graph comms SDK: application-hosted media calls (`MediaPlatformSettings` + `AppHostedMediaConfig`):  
  https://microsoftgraph.github.io/microsoft-graph-comms-samples/docs/articles/calls/appHostedMediaCalls.html
- **S15** – Azure Speech SDK: audio input streams (push/pull streaming):  
  https://learn.microsoft.com/en-us/azure/ai-services/speech-service/how-to-use-audio-input-streams
- **S16** – Azure Speech SDK: `PushAudioInputStream()` default format (16 kHz, 16-bit, mono PCM):  
  https://learn.microsoft.com/en-us/dotnet/api/microsoft.cognitiveservices.speech.audio.pushaudioinputstream.-ctor?view=azure-dotnet
- **S17** – Azure Speech SDK: `StartContinuousRecognitionAsync` reference:  
  https://learn.microsoft.com/en-us/dotnet/api/microsoft.cognitiveservices.speech.speechrecognizer.startcontinuousrecognitionasync?view=azure-dotnet
- **S18** – Azure Speech SDK: continuous recognition events (`Recognizing`, `Recognized`, `Canceled`):  
  https://learn.microsoft.com/en-us/azure/ai-services/speech-service/how-to-recognize-speech
- **S19** – Teams sample (helpful for manifest upload + tunneling patterns):  
  https://learn.microsoft.com/en-us/samples/officedev/microsoft-teams-samples/officedev-microsoft-teams-samples-bot-calling-meeting-csharp/
- **S20** – Tenant enablement reality check (example: error 7504 requires provisioning/support):  
  https://learn.microsoft.com/en-us/answers/questions/2262756/insufficient-enterprise-tenant-permissions-using-cl
- **S21** – Test and debug a Teams bot locally (IDE + custom app upload note):  
  https://learn.microsoft.com/en-us/microsoftteams/platform/bots/how-to/debug/locally-with-an-ide
- **S22** – Microsoft Graph comms samples: Teams Voice Echo Bot (audio stream example in the official repo):  
  https://github.com/microsoftgraph/microsoft-graph-comms-samples/tree/master/Samples/PublicSamples/EchoBot
---

## Golden path (fastest functional POC)

If you only do one thing, do this path:

0. **Confirm your tenant is provisioned/enabled for Teams calling/meeting bots + real-time media** (preview capability; if you hit error `7504`, you need support/provisioning before anything else).  
   Source: S4, S20.
1. **Use Microsoft’s application-hosted media model** (C#, Windows) to get raw audio frames.  
   Source: S3, S4, S5.
2. **Base your bot on a Microsoft local media sample** (Graph comms samples “LocalMediaSamples”).  
   Source: S4, S10, S11, S12.
3. **Expose two endpoints from your dev machine:**
   - HTTPS signaling webhook (Teams/Graph → your bot): use ngrok HTTP tunnel  
   - TCP media endpoint (Teams/Graph ↔ your media port): use ngrok TCP tunnel + your DNS + wildcard cert  
   Source: S2.
4. **Join a meeting** by sending your bot a join URL (or add the bot to the meeting once the Teams app package is uploaded).  
   Source: S6, S7, S19.
5. **On `AudioMediaReceived`, push audio frames into Azure Speech continuous recognition**, then POST/WS the transcript to your Python agent.  
   Source: S5, S15, S16, S17, S18.

---

# PART A — Build the bot that can join meetings and receive real-time audio

## A0. Prerequisites (don’t skip)

### A0.1. OS + runtime requirements
- You must develop the app-hosted media bot in **C#/.NET** and run it in a **Windows environment**.  
  Sources: S3, S4.
- The bot must use a recent `Microsoft.Graph.Communications.Calls.Media` library version; older versions are deprecated and stop working.  
  Source: S4.
- Application-hosted media bots can’t be deployed as an Azure Web App (so for POC, run locally; later, use a Windows VM).  
  Source: S4.
- Bot Framework Emulator doesn’t support application-hosted media bots.  
  Source: S4.

### A0.2. Tools you will actually use
- Visual Studio 2022 (or later) on Windows.
- .NET SDK (sample code commonly targets .NET 6 per Microsoft samples).  
  Source: S4, S19.
- A tunneling tool that supports:
  - HTTPS for webhook (signaling)
  - TCP for media  
  ngrok supports TCP tunnels; calls/meeting bots need TCP.  
  Source: S2, S19.
- A DNS zone you can edit (even a throwaway subdomain) — required for app-hosted media with ngrok TCP.  
  Source: S2.
- A publicly trusted SSL certificate matching the DNS name(s) you will use for the media endpoint (wildcard is easiest).  
  Source: S2.


### A0.3. HARD GATE: confirm your tenant is enabled for Calls/Meetings + Real-time Media

Before you spend time on DNS + certificates, understand this reality:

- The **Real-time Media Platform for bots** is explicitly called out as **developer preview** in the app-hosted media requirements.  
  Source: S4.
- In practice, some tenants hit **hard authorization/provisioning failures** (for example error `7504` / “Insufficient enterprise tenant permissions”) when trying to use Cloud Communications calling APIs.  
  Source: S20.

**What you do right now (fastest check):**

1. Proceed through **A1 (app registration + permissions)** and **A2 (Azure Bot + Teams calling enabled)**.  
   Source: S1.
2. Run the Microsoft sample (A4–A9). If your bot cannot create/join the call and you see an error like `7504` / “Insufficient enterprise tenant permissions”, stop.  
   Source: S20.
3. Open a Microsoft support request to have Cloud Communications calling enabled/provisioned for your tenant (include Tenant ID + App (client) ID and that you’re building a Teams calls/meetings bot using Graph Cloud Communications).  
   Source: S20.

> This is not a “code bug”; until the tenant is provisioned, you can’t complete the POC.

---

## A1. Microsoft Entra ID (Azure AD) app registration

> This app registration is the identity your bot uses to call Microsoft Graph cloud communications APIs.

### A1.1. Create the app registration
1. Azure Portal → **App registrations** → **New registration**.  
2. Account type: **Single tenant** (fastest for internal POC).  
3. Record:
   - Application (client) ID
   - Directory (tenant) ID  
**Source:** S1.

### A1.2. Create a client secret
1. App registration → **Certificates & secrets** → **New client secret**.
2. Copy the secret value now.  
**Source:** S1.

### A1.3. Add required Microsoft Graph application permissions
For “join meeting + access raw media frames”, the key permissions are:

- `Calls.JoinGroupCall.All` (join meetings as app)  
- `Calls.AccessMedia.All` (access real-time media streams)  

Optional (only if you need them):
- `Calls.JoinGroupCallAsGuest.All` (join as guest)  
- `Calls.InitiateGroupCall.All` / `Calls.Initiate.All` (if you place outbound calls)  
- `OnlineMeetings.Read.All` / `OnlineMeetings.ReadWrite.All` (if you read or create meetings via Graph; not required if you always supply a join URL manually)  

Admin consent is required for these application permissions.  
**Sources:** S1, S6.

✅ After permissions are added, click **Grant admin consent**.  
**Source:** S1.

---

## A2. Create the Azure Bot resource and enable Calling

> A calls/meetings bot is still a Teams bot. You enable “Calling” and provide a webhook for call events.

### A2.1. Create Azure Bot
1. Azure Portal → Create → **Azure Bot** resource.
2. **Use existing app registration** (the one from A1).  
3. Create.  
**Source:** S1 (concept), S19 (practical sample flow).

### A2.2. Add the Teams channel and enable calling
1. Azure Bot resource → **Channels** → add/enable **Microsoft Teams**.
2. In Teams channel → **Calling** tab:
   - Check **Enable calling**
   - Set **Webhook (for calling)** = your public HTTPS signaling endpoint (you’ll set it after ngrok is running).  
**Sources:** S1, S19.

---

## A3. Create the Teams app package (internal upload only)

> This is required because calls/meetings bots must declare support in the Teams manifest.

### A3.1. Manifest essentials
In `manifest.json`, ensure:

- `bots[0].supportsCalling: true`
- `bots[0].supportsVideo: false` (for audio-only POC; set true only if you implement video)
- Use a schema that validates these keys (docs show v1.11+).  
**Source:** S1.

### A3.2. validDomains
Add the domain(s) your bot uses (your ngrok HTTPS domain for signaling; and any domain your bot loads content from).  
**Source:** S19.

### A3.3. Upload to Teams (sideload)
1. Zip the **contents** of the manifest folder (manifest + icons at the zip root).
2. Teams → Apps → **Upload a custom app** → select the zip → Add.  
**Sources:** S19, S21.

---

## A4. Get a Microsoft application-hosted media sample (C#) and build it

### A4.1. Pick a sample that actually gives raw audio frames
You need an **application-hosted media bot**, not only service-hosted “record prompt” APIs. Application-hosted media gives “raw” access to audio/video streams.  
**Sources:** S3, S4, S5, S10.

Recommended baseline:
- Microsoft Graph communications samples → **Local media sample(s)**  
- Microsoft Graph communications samples → **EchoBot (“Teams Voice Echo Bot”)** (useful because it’s explicitly built around the Teams audio stream)  
**Sources:** S4, S22.

### A4.2. Clone the repo and open the solution
On your dev machine:

```powershell
cd C:\dev
git clone https://github.com/microsoftgraph/microsoft-graph-comms-samples.git
```

**Fast way to locate the right sample solution in the repo (no guessing file paths):**

```powershell
cd C:\dev\microsoft-graph-comms-samples
Get-ChildItem -Recurse -Filter *.sln | Select-Object FullName
```

- If you want the most direct “audio stream” baseline, look for an **EchoBot** solution/folder.  
  Source: S22.
- Otherwise, pick any sample that includes an **application-hosted media** session (local media).  
  Sources: S4, S14.


Then open the solution you chose (for example `EchoBot.sln` if you’re using that baseline; otherwise a LocalMedia sample `.sln`) in Visual Studio.

**Source guidance for sample setup:** S11, S12, S14.

### A4.3. Update NuGet packages if the sample doesn’t restore/build
If the sample doesn’t build, the most common root cause is an outdated media library. Microsoft explicitly requires the bot to run on a “recent” `Microsoft.Graph.Communications.Calls.Media` version (not older than ~3 months).  
**Source:** S4.

In Visual Studio:
- Right-click solution → **Manage NuGet Packages** → update `Microsoft.Graph.Communications.Calls.Media` and related `Microsoft.Graph.Communications.*` packages.

---

## A5. Local dev networking: ngrok + DNS + wildcard cert (this is the make-or-break step)

### A5.1. Why this step exists
Calls/meetings bots require:
- **HTTPS** signaling (Graph → your webhook)  
- **TCP** media for application-hosted media (Graph ↔ your media port)  

ngrok supports both.  
**Source:** S2.

### A5.2. Create DNS CNAME(s) for media
Microsoft’s local testing guidance:

- ngrok’s TCP hosts are fixed: `0.tcp.ngrok.io`, `1.tcp.ngrok.io`, …  
- You must create DNS CNAME records you own that point to these fixed hosts.  
Example (choose any domain you control):
- `0.botpoc.example.com` CNAME → `0.tcp.ngrok.io`  
- `1.botpoc.example.com` CNAME → `1.tcp.ngrok.io`  
**Source:** S2.

> For a single POC bot instance, you can usually get away with only the `0.*` hostname.

### A5.3. Obtain an SSL certificate matching your media DNS name
Microsoft’s guidance for local app-hosted media:
- Use an SSL certificate issued to a **wildcard domain** (ex: `*.botpoc.example.com`)
- The media SDK validates this certificate; it must match your public media URL (for example `0.botpoc.example.com`).  
**Source:** S2.

Install the certificate into **Local Machine → Personal (My)** and record its thumbprint.  
**Source:** S2.

### A5.4. Create ngrok config
Create `ngrok.yml`:

```yaml
version: "2"
authtoken: <YOUR_NGROK_AUTHTOKEN>

tunnels:
  signaling:
    proto: http
    addr: 9441
    host_header: "localhost:9441"

  media:
    proto: tcp
    addr: 8445
```

Why this shape:
- Calls/meetings signaling events come as HTTP POSTs to your bot's calling endpoint.  
- App-hosted media uses TCP tunnels.  
**Source:** S2.

### A5.5. Start ngrok
```powershell
ngrok start --all --config C:\ngrok\ngrok.yml
```

Record:
- **Signaling public HTTPS URL** (example: `https://abc123.ngrok-free.app`)
- **Media public TCP host/port** (example: `tcp://0.tcp.ngrok.io:12345`)  
**Source:** S2.

### A5.6. Convert ngrok TCP output → the values your bot config needs
If ngrok prints:
- `tcp://0.tcp.ngrok.io:12345`

And your DNS has:
- `0.botpoc.example.com` CNAME → `0.tcp.ngrok.io`

Then your bot media config should use:
- `ServiceFqdn = "0.botpoc.example.com"`
- `InstancePublicPort = 12345`
- `InstanceInternalPort = 8445`
- `CertificateThumbprint = <thumbprint of *.botpoc.example.com cert>`  
**Source:** S2.

---

## A6. Point Azure Bot’s “Webhook (for calling)” at your ngrok signaling endpoint
In Azure Bot → Teams channel → Calling tab:

- Webhook (for calling):  
  `https://<your-ngrok-https-domain>/<your-calling-notification-path>`

Example:
- `https://abc123.ngrok-free.app/api/calling`

**Sources:** S1, S19.

---

## A7. Configure your bot sample for application-hosted media

This is the part where your sample must:
1. Use `SetNotificationUrl(...)` to the HTTPS signaling URL
2. Provide `MediaPlatformSettings` with your media TCP settings and cert thumbprint
3. Create/join calls using `AppHostedMediaConfig` and an `IMediaSession`  
**Sources:** S2, S14.

### A7.1. Notification URL (signaling)
Set your bot’s notification/callback URL to the **public HTTPS** ngrok domain and your notification endpoint path.  
**Source:** S2.

### A7.2. MediaPlatformSettings (app-hosted media)
Microsoft’s local-testing doc gives the required fields:

- CertificateThumbprint
- InstanceInternalPort
- InstancePublicPort
- InstancePublicIPAddress (0.0.0.0 in their example)
- ServiceFqdn  
**Source:** S2.

Graph comms SDK docs explain that you pass `MediaPlatformSettings` into the communications client builder and use `AppHostedMediaConfig` when creating a call.  
**Source:** S14.

### A7.3. Using the repo’s helper scripts (if available)
If your sample repo provides a `configure_cloud.ps1` script (as your original doc uses), you can still use it — but you must provide:
- **-dns** = your **custom DNS name** (example: `0.botpoc.example.com`) not `0.tcp.ngrok.io`
- **-thumb** = the thumbprint of the cert that matches that DNS
- **-tcp** = the ngrok remote media port (example `12345`)  

When in doubt, validate the resulting config matches the fields in **A5.6**.  
**Source:** S2 (authoritative rules).

---

## A8. Run the bot locally
- Run from Visual Studio.
- For many local media samples, running elevated (Admin) avoids low-level socket/bind errors. (Practical note.)
- Confirm logs indicate the bot is listening on:
  - signaling port (9441 in this guide)
  - media internal port (8445)  
**Source:** Setup pattern in S2/S11/S12.

---

## A9. Join a Teams meeting

### A9.1. Get a meeting join URL
In Teams, copy the “Join the meeting” link.

Graph documents that join URLs can be used to populate meeting and chat info.  
**Source:** S7.

### A9.2. Join the meeting (typical local media sample approach)
Most local media samples expose a simple HTTP endpoint you call with:
- `JoinUrl`
- Display name for the bot participant

Example PowerShell (adjust URL/path to match your sample’s join endpoint):

```powershell
$body = @{
  JoinUrl = "https://teams.microsoft.com/l/meetup-join/..."
  DisplayName = "POC Transcription Bot"
} | ConvertTo-Json

Invoke-RestMethod `
  -Uri "https://<your-ngrok-https-domain>/joinCall" `
  -Method Post `
  -ContentType "application/json" `
  -Body $body
```

Joining a meeting is implemented as a `POST /communications/calls` (create call) under the hood.  
**Source:** S6.

---

# PART B — Transcribe the live audio and stream transcripts to Python

## B0. Confirm you are receiving audio frames
Microsoft Teams real-time media delivers **20 ms** audio frames and around **50 frames/sec**.  
**Source:** S5.

In your audio handler (event name varies per sample), log:
- timestamp
- buffer length
- running frames/sec

This confirms you are truly receiving real-time audio.

---

## B1. Fastest transcription POC: Azure Speech SDK (C#) with PushAudioInputStream

### B1.1. Why PushAudioInputStream
Speech SDK supports streaming audio into the recognizer using audio input streams.  
**Source:** S15.

`PushAudioInputStream()` default format is **16 kHz, 16-bit, mono PCM**.  
**Source:** S16.

Continuous recognition is started with `StartContinuousRecognitionAsync()` and results come via events (Recognizing/Recognized/Canceled).  
**Sources:** S17, S18.

### B1.2. Add the Speech SDK to your bot project
In the bot’s C# project:
- Add NuGet: `Microsoft.CognitiveServices.Speech`

(Use the latest stable version compatible with your target framework.)

---

## B2. Reference implementation: C# transcriber that accepts PCM frames and emits transcript events

> This code is designed to be copy/paste-able into a POC.
> You will still need to wire it into your sample’s actual audio event.

### B2.1. C# — Transcriber class

```csharp
using System;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;
using Microsoft.CognitiveServices.Speech;
using Microsoft.CognitiveServices.Speech.Audio;

public sealed class AzureSpeechRealtimeTranscriber : IAsyncDisposable
{
    private readonly SpeechRecognizer _recognizer;
    private readonly PushAudioInputStream _pushStream;
    private readonly HttpClient _http;
    private readonly Uri _pythonEndpoint;

    public AzureSpeechRealtimeTranscriber(
        string speechKey,
        string speechRegion,
        string recognitionLanguage,
        Uri pythonEndpoint)
    {
        _pythonEndpoint = pythonEndpoint;
        _http = new HttpClient();

        var speechConfig = SpeechConfig.FromSubscription(speechKey, speechRegion);
        speechConfig.SpeechRecognitionLanguage = recognitionLanguage;

        // Default is 16 kHz, 16-bit, mono PCM (PushAudioInputStream()).
        _pushStream = AudioInputStream.CreatePushStream();
        var audioConfig = AudioConfig.FromStreamInput(_pushStream);

        _recognizer = new SpeechRecognizer(speechConfig, audioConfig);

        _recognizer.Recognizing += async (_, e) =>
            await PublishAsync(kind: "partial", text: e.Result?.Text);

        _recognizer.Recognized += async (_, e) =>
            await PublishAsync(kind: "final", text: e.Result?.Text);

        _recognizer.Canceled += async (_, e) =>
            await PublishAsync(kind: "error", text: $"{e.Reason}: {e.ErrorDetails}");

        _recognizer.SessionStarted += async (_, __) =>
            await PublishAsync(kind: "status", text: "speech_session_started");

        _recognizer.SessionStopped += async (_, __) =>
            await PublishAsync(kind: "status", text: "speech_session_stopped");
    }

    public Task StartAsync() => _recognizer.StartContinuousRecognitionAsync();

    public Task StopAsync() => _recognizer.StopContinuousRecognitionAsync();

    public void PushPcm16k16bitMono(byte[] pcm)
    {
        if (pcm == null || pcm.Length == 0) return;
        _pushStream.Write(pcm);
    }

    private async Task PublishAsync(string kind, string? text)
    {
        if (string.IsNullOrWhiteSpace(text)) return;

        var payload = new
        {
            kind,
            text,
            tsUtc = DateTime.UtcNow.ToString("o"),
        };

        var json = JsonSerializer.Serialize(payload);
        using var content = new StringContent(json, Encoding.UTF8, "application/json");
        await _http.PostAsync(_pythonEndpoint, content).ConfigureAwait(false);
    }

    public async ValueTask DisposeAsync()
    {
        try { await StopAsync().ConfigureAwait(false); } catch { /* ignore */ }

        _recognizer.Dispose();
        _pushStream.Dispose();
        _http.Dispose();
    }
}
```

**Why this is correct:**
- Uses streaming audio input (S15)
- Uses default `PushAudioInputStream` PCM format (S16)
- Uses continuous recognition start method (S17)
- Uses event-driven results (S18)

---

## B3. Wire transcription into the Teams media bot’s audio callback

### B3.1. In your audio receive handler
Your sample should have something like an `AudioMediaReceived` event on an `AudioSocket`. In that handler:

1. Copy the incoming unmanaged buffer into a managed `byte[]`
2. Feed those bytes into `PushPcm16k16bitMono(...)`

Pseudo-wiring:

```csharp
private AzureSpeechRealtimeTranscriber _stt;

public async Task StartSttAsync()
{
    _stt = new AzureSpeechRealtimeTranscriber(
        speechKey: Environment.GetEnvironmentVariable("SPEECH_KEY"),
        speechRegion: Environment.GetEnvironmentVariable("SPEECH_REGION"),
        recognitionLanguage: "en-US",
        pythonEndpoint: new Uri("http://localhost:8765/transcript"));

    await _stt.StartAsync();
}

// In your audio callback:
private void OnAudioReceived(byte[] pcmFrame)
{
    _stt?.PushPcm16k16bitMono(pcmFrame);
}
```

**Source:** Real-time media can be processed frame-by-frame for real-time speech recognition. (S5)

---

## B4. Python receiver (minimal) — FastAPI

### B4.1. Install deps
```bash
pip install fastapi uvicorn
```

### B4.2. Run server
```python
# transcript_sink.py
from fastapi import FastAPI, Request
import uvicorn

app = FastAPI()

@app.post("/transcript")
async def transcript(req: Request):
    payload = await req.json()
    print(payload)
    # TODO: forward to your agent framework here
    return {"ok": True}

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765)
```

Run:
```bash
python transcript_sink.py
```

---

## B5. Minimal “agent hook” example (replace with your framework)

Inside the FastAPI handler:

```python
text = payload.get("text", "")
kind = payload.get("kind", "final")

if kind in ("partial", "final"):
    # Example: push into your agent input queue
    agent_queue.put_nowait({"kind": kind, "text": text})
```

---

# PART C — Practical verification checklist (functional-only)

## C1. Bot registration sanity checks
- Azure Bot → Teams channel → Calling enabled + webhook points to your ngrok HTTPS endpoint.  
  **Source:** S1, S19.
- App registration has `Calls.JoinGroupCall.All` + `Calls.AccessMedia.All` granted with admin consent.  
  **Source:** S1, S6.

## C2. Media connectivity sanity checks (the #1 failure point)
- DNS CNAME exists: `0.botpoc.example.com` → `0.tcp.ngrok.io`  
  **Source:** S2.
- Bot config:
  - ServiceFqdn = `0.botpoc.example.com`
  - InstancePublicPort = ngrok TCP port
  - InstanceInternalPort = local media port (8445)
  - CertificateThumbprint matches a cert for `*.botpoc.example.com`  
  **Source:** S2.

## C3. Live audio + STT sanity checks
- You see audio callback firing ~50 times/sec (20 ms frames).  
  **Source:** S5.
- Azure Speech emits `Recognizing`/`Recognized` events.  
  **Source:** S18.

## C4. Python consumption sanity checks
- Python endpoint prints incoming JSON messages in real time.

---

# Appendix — (Optional) About `updateRecordingStatus` and “recording” restrictions

Microsoft Graph documentation includes an explicit restriction: if your app “records or otherwise persists media content from calls or meetings … or data derived from that media content”, you must first call `updateRecordingStatus` and receive success.  
**Source:** S8, S9, S1.

`updateRecordingStatus` is described as requiring the Teams policy-based recording solution.  
**Source:** S9.

This guide is focused on building the POC data path (live audio → transcript → python). If your scenario crosses into “recording” (including persistence), align with these Graph requirements before you treat the transcript as a stored/recorded artifact.

---


---

# PART D — Copy/paste runbook (fastest path to **live transcripts** in your Python agent)

> This section is intentionally verbose and “do-this-then-that”. It’s designed so an AI Engineer + Global Admin can execute without hunting through docs.
>
> **Everything here is derived from Parts A–C**; it’s just repackaged as a runbook.

**Primary sources for this runbook:** S1, S2, S4, S5, S6, S11, S14, S15–S18, S20, S21.

## D0. One-time worksheet (fill these values once)

Create a scratchpad file and fill in values as you go:

```text
TENANT_ID=
APP_CLIENT_ID=
APP_CLIENT_SECRET=

# Public signaling (HTTPS) - from ngrok
SIGNALING_HTTPS_BASE=https://<xxxxx>.ngrok-free.app
SIGNALING_PATH=/api/calling   # (or whatever your sample uses)

# Media DNS you own (CNAME to ngrok TCP host)
MEDIA_FQDN_0=0.botpoc.example.com
MEDIA_CNAME_TARGET_0=0.tcp.ngrok.io

# Ports
LOCAL_SIGNALING_PORT=9441
LOCAL_MEDIA_PORT=8445
NGROK_MEDIA_PUBLIC_PORT=<the random port ngrok prints after tcp://0.tcp.ngrok.io:PORT>

# Certificate
MEDIA_CERT_SUBJECT=*.botpoc.example.com
MEDIA_CERT_THUMBPRINT=

# Azure Speech
SPEECH_KEY=
SPEECH_REGION=
SPEECH_RECO_LANG=en-US

# Python transcript sink
PYTHON_TRANSCRIPT_ENDPOINT=http://127.0.0.1:8765/transcript
```

**Sources:** S2 (DNS+cert+ngrok rules), S5 (audio format), S15–S16 (Speech audio format defaults).

## D1. HARD GATE: make sure your tenant can actually use these APIs

This capability is not “just code.” It’s tied to Teams + Graph Cloud Communications enablement and (per Microsoft docs) the real-time media platform is **developer preview**.

### D1.1 What “blocked” looks like
- You get an error such as `7504` / **Insufficient enterprise tenant permissions** when attempting to create/join calls via Microsoft Graph communications APIs.
- You cannot proceed until the tenant is enabled/provisioned (support request).

**Source:** S20 (example + guidance), S4 (preview).

### D1.2 What to do if you are blocked (fastest path)
1. Capture the full error payload and correlation/request IDs from your bot logs or Graph responses.
2. Open a Microsoft support request and explicitly say you need Cloud Communications calling for a Teams calls/meetings bot (Graph Communications / real-time media). Include:
   - Tenant ID
   - App (client) ID
   - The exact error (`7504`) and time
3. Only come back to DNS/cert/ngrok after Microsoft confirms the tenant is enabled.

**Source:** S20.

## D2. Make sure you can sideload an internal Teams app (no Store publishing)

Even for internal-only POC, you still need to upload a Teams app package that declares a calling bot.

### D2.1 Confirm “Upload a custom app” is available
- In the Teams client: go to **Apps** and confirm you see **Upload a custom app**.
- If you do not see it, a Teams admin must enable custom app upload / app permission policy settings so you can install the app internally.

**Source:** S21 (local debugging doc calls out enabling custom app upload), S1 (manifest required for calling bots).

## D3. Domain + DNS for media (required)

Application-hosted media bots require a **DNS name you control** that maps to ngrok’s fixed TCP host(s).

### D3.1 If you already own a domain
Use a dedicated subdomain so you can create the `0.` and `1.` hostnames cleanly (example: `botpoc.example.com`).

### D3.2 If you do not own a domain
Register any domain (any registrar is fine). Then create a subdomain you can freely edit for this POC.

> This guide cannot provide registrar-specific click paths (they vary), but the DNS record you must create is unambiguous and documented by Microsoft.

### D3.3 Create the required DNS CNAME record(s)
Create these records in your DNS zone:

- `0.botpoc.example.com` **CNAME** → `0.tcp.ngrok.io`
- (Optional) `1.botpoc.example.com` **CNAME** → `1.tcp.ngrok.io`

**Source:** S2 (explicit requirement for DNS CNAME(s) mapping to ngrok fixed TCP hosts).

### D3.4 Verify DNS resolves before proceeding
On Windows PowerShell:

```powershell
nslookup 0.botpoc.example.com
```

Expected: the answer should show it aliases to `0.tcp.ngrok.io` (or resolves to an address via that chain).

**Source:** S2 (the mapping is required; verifying it is a functional check).

## D4. Public SSL certificate for the media DNS name (required)

### D4.1 The rule you must satisfy
- The **media endpoint** must present a certificate that matches the **ServiceFqdn** you configure (for example `0.botpoc.example.com`).
- Microsoft’s local testing guidance recommends using a **wildcard certificate** (for example `*.botpoc.example.com`).

**Source:** S2 (certificate + wildcard guidance).

### D4.2 Get the certificate (fastest practical options)
Pick ONE:

1. **Use an existing corporate wildcard cert** for your subdomain (fastest if your org already issues public-trusted certs).
2. **Buy a wildcard cert** from any public CA (fastest if you can purchase quickly).
3. **Use Let’s Encrypt wildcard** (free, but requires DNS validation setup at your DNS provider).

> This guide does not prescribe one CA—Microsoft only cares that it is publicly trusted and matches the domain.  
> **Source:** S2.

### D4.3 Install the cert into the Windows Local Machine certificate store
1. Open `mmc.exe`
2. Add the **Certificates** snap-in for **Computer account** → **Local computer**
3. Import the `.pfx` into: **Certificates (Local Computer) → Personal → Certificates**
4. Confirm it appears and shows **You have a private key that corresponds to this certificate**.

**Source:** S2 (certificate must be installed; Media SDK validates by thumbprint).

### D4.4 Capture the thumbprint (you will paste this into bot config)
PowerShell (run normally):

```powershell
Get-ChildItem Cert:\LocalMachine\My |
  Sort-Object NotAfter -Descending |
  Select-Object Thumbprint, Subject, NotAfter |
  Format-Table -AutoSize
```

Record the thumbprint for the wildcard cert that covers `0.botpoc.example.com`.

**Source:** S2.

## D5. Start ngrok (HTTPS signaling + TCP media)

### D5.1 Install and authenticate ngrok
1. Download ngrok and install it on your Windows dev machine.
2. Authenticate with your ngrok authtoken.

*(ngrok steps are vendor-specific; the functional requirement is simply: you must be able to run an HTTPS tunnel and a TCP tunnel.)*

**Source:** S2 (requires HTTPS + TCP; ngrok is used in Microsoft’s local testing guide).

### D5.2 Create `ngrok.yml`
Example (matches Microsoft guidance):

```yaml
version: "2"
authtoken: <YOUR_NGROK_AUTHTOKEN>

tunnels:
  signaling:
    proto: http
    addr: 9441

  media:
    proto: tcp
    addr: 8445
```

Notes:
- Signaling uses `proto: http` locally because ngrok terminates TLS and exposes **public HTTPS**.  
- Media uses `proto: tcp` because app-hosted media requires TCP.  

**Source:** S2 (local testing configuration), S11 (similar examples).

### D5.3 Start ngrok
```powershell
ngrok start --all --config C:\ngrok\ngrok.yml
```

Record these two outputs from the ngrok console:

1. The HTTPS forwarding URL (example): `https://abc123.ngrok-free.app`
2. The TCP forwarding URL (example): `tcp://0.tcp.ngrok.io:12345`

**Source:** S2.

### D5.4 Convert ngrok TCP output to your bot’s media settings
If ngrok shows:
- `tcp://0.tcp.ngrok.io:12345`

And your DNS has:
- `0.botpoc.example.com` CNAME → `0.tcp.ngrok.io`

Then your bot must be configured with:
- `ServiceFqdn = 0.botpoc.example.com`
- `InstancePublicPort = 12345`
- `InstanceInternalPort = 8445` (your local media listening port)
- `CertificateThumbprint = <thumbprint for *.botpoc.example.com>`

**Source:** S2, S11.

## D6. Microsoft Entra ID app registration (identity + Graph permissions)

### D6.1 Create the app registration
1. In Azure Portal: **Microsoft Entra ID** → **App registrations** → **New registration**.
2. Name: `TeamsMediaBotPOC` (any name).
3. Supported account type: **Single tenant** (fastest for internal POC).
4. Create.
5. Copy/paste:
   - **Application (client) ID** → `APP_CLIENT_ID`
   - **Directory (tenant) ID** → `TENANT_ID`

**Source:** S1, S13.

### D6.2 Create a client secret
1. App registration → **Certificates & secrets** → **New client secret**.
2. Create and copy the **secret value** immediately.
3. Store it as `APP_CLIENT_SECRET` for local dev.

**Source:** S1, S13.

### D6.3 Add Microsoft Graph *Application* permissions
Go to: App registration → **API permissions** → **Add a permission** → **Microsoft Graph** → **Application permissions**.

Add at minimum:
- `Calls.AccessMedia.All`  
- `Calls.JoinGroupCall.All`

Then click **Grant admin consent**.

**Source:** S1 (Teams calling bot permission requirements), S6 (create call permissions list).

## D7. Azure Bot resource + Teams channel + Calling enabled

### D7.1 Create the Azure Bot resource
1. Azure Portal → Create resource → search **Azure Bot** → Create.
2. Use your existing **App registration** (client ID) from D6.
3. Create.

**Source:** S1 (calls/meetings bot registration flow).

### D7.2 Add the Microsoft Teams channel
1. Open your Azure Bot resource.
2. Go to **Channels**.
3. Add/enable **Microsoft Teams**.

**Source:** S1.

### D7.3 Enable Calling and set the Calling webhook
1. In the Teams channel configuration, find the **Calling** tab.
2. Check **Enable calling**.
3. Set **Webhook (for calling)** to your ngrok HTTPS URL + your calling path (example):

```text
https://abc123.ngrok-free.app/api/calling
```

4. Apply/save.

**Source:** S1 (calling bots require enabling calling + webhook), S2 (local dev uses ngrok HTTPS).

## D8. Teams app package (manifest) for internal install

### D8.1 Why you need this
Teams calling bots must declare calling support in the Teams app manifest (`supportsCalling`).

**Source:** S1.

### D8.2 Minimal `manifest.json` you can start from (audio-only calling bot)
> Paste this into `manifest/manifest.json` and then edit placeholders.

```json
{
  "$schema": "https://developer.microsoft.com/json-schemas/teams/v1.11/MicrosoftTeams.schema.json",
  "manifestVersion": "1.11",
  "version": "1.0.0",
  "id": "<YOUR-TEAMS-APP-PACKAGE-GUID>",
  "packageName": "com.yourorg.teamsmediabotpoc",
  "developer": {
    "name": "Your Org",
    "websiteUrl": "https://example.com",
    "privacyUrl": "https://example.com/privacy",
    "termsOfUseUrl": "https://example.com/terms"
  },
  "name": {
    "short": "Teams Media Bot POC",
    "full": "Teams Media Bot POC"
  },
  "description": {
    "short": "Internal POC: join meeting, stream audio, transcribe",
    "full": "Internal POC: join Teams meeting, stream audio in real time, transcribe, send to Python agent."
  },
  "icons": {
    "outline": "outline.png",
    "color": "color.png"
  },
  "accentColor": "#FFFFFF",
  "bots": [
    {
      "botId": "<APP_CLIENT_ID>",
      "scopes": ["personal", "team", "groupchat"],
      "supportsFiles": false,
      "isNotificationOnly": false,
      "supportsCalling": true,
      "supportsVideo": false
    }
  ],
  "permissions": ["identity", "messageTeamMembers"],
  "validDomains": [
    "<YOUR_NGROK_SUBDOMAIN>.ngrok-free.app",
    "0.botpoc.example.com"
  ]
}
```

Notes:
- The only fields that are truly critical for calling are the `bots` block with `supportsCalling: true`.  
- You can keep this minimal for internal sideloading; you are **not** publishing to the Teams Store.

**Source:** S1 (manifest keys for calling), S21 (internal testing via custom app upload).

### D8.3 Package the Teams app zip
Your zip must have these files at the root:

```text
manifest.json
color.png
outline.png
```

Then in Teams:
- **Apps** → **Upload a custom app** → select the zip → install

**Sources:** S21, S19.

## D9. Bot code: use a Microsoft app-hosted media sample and configure it

At this point you have:
- ngrok HTTPS + TCP running (D5)
- DNS CNAME for media (D3)
- Wildcard cert installed and thumbprint (D4)
- Entra app with Graph permissions (D6)
- Azure Bot with Teams calling enabled + webhook set (D7)
- Teams app package installed internally (D8)

### D9.1 Get the official Microsoft Graph communications samples
```powershell
cd C:\dev
git clone https://github.com/microsoftgraph/microsoft-graph-comms-samples.git
```

**Sources:** S10, S11, S12.

### D9.2 Open the sample solution
Fastest way to find candidate solutions:

```powershell
cd C:\dev\microsoft-graph-comms-samples
Get-ChildItem -Recurse -Filter *.sln | Select-Object FullName
```

- Prefer a sample that clearly references **application-hosted media** / **local media** / `MediaPlatformSettings`.  
  Sources: S4, S14.
- If you found EchoBot, it is explicitly an audio-stream sample.  
  Source: S22.

### D9.3 Configure the sample: Notification URL (signaling)
Your bot must expose a public HTTPS callback URL for notifications.

**Rule:** NotificationUrl must be HTTPS; for local dev without end-to-end encryption, the bot can listen on HTTP locally because ngrok terminates TLS.  
**Source:** S2, S11.

Set the notification URL in the sample code/config to:

```text
https://<YOUR_NGROK_SUBDOMAIN>.ngrok-free.app/api/calling
```

**Sources:** S2, S11.

### D9.4 Configure the sample: MediaPlatformSettings (app-hosted media)
Find where the sample sets `MediaPlatformSettings` and set these fields:

- `ApplicationId` = your `APP_CLIENT_ID`
- `CertificateThumbprint` = your wildcard certificate thumbprint
- `InstanceInternalPort` = `LOCAL_MEDIA_PORT` (8445)
- `InstancePublicPort` = the ngrok TCP port (the `:12345` from `tcp://0.tcp.ngrok.io:12345`)
- `ServiceFqdn` = your DNS name (`0.botpoc.example.com`)
- `InstancePublicIPAddress` = `0.0.0.0` (commonly used in docs/examples)

These exact fields are shown in Microsoft’s local testing guidance and sample docs.  
**Sources:** S2, S11, S14.

### D9.5 Run the bot and confirm it is listening
Run from Visual Studio and confirm:
- The bot is listening on your local signaling port (9441 in this guide)
- The bot is listening on your local media port (8445 in this guide)

**Sources:** S2, S11 (port mapping examples).

## D10. Join a Teams meeting (so you can receive live audio)

### D10.1 Create a test meeting and get the join URL
1. In Teams, create a meeting (Meet now or scheduled).
2. Copy the **Join URL** (the full `https://teams.microsoft.com/l/meetup-join/...` link).

(No special source required—this is standard Teams UI.)

### D10.2 Join the meeting using your bot sample
Most Microsoft samples provide a “join” action (often an HTTP endpoint) that you call with a join URL.

Because sample implementations vary, the robust way is:

1. Search your bot project for `join` endpoints/routes:

```powershell
cd C:\dev\microsoft-graph-comms-samples
Select-String -Path .\**\*.cs -Pattern "joinCall|join.*meeting|meeting join|JoinMeeting"
```

2. If the sample provides a `/joinCall`-style endpoint, call it with your join URL.

Example PowerShell (adjust URL/path to match your sample):

```powershell
$body = @{ joinUrl = "<PASTE_TEAMS_JOIN_URL>" } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "https://<YOUR_NGROK_SUBDOMAIN>.ngrok-free.app/joinCall" -Body $body -ContentType "application/json"
```

3. Watch bot logs for call state changes and (eventually) media connection.

**Sources:** S6 (joining meetings via create call), S14 (app-hosted media calls), S11 (sample testing patterns).

### D10.3 (Debug) What the bot ultimately does under the hood (Graph create call)
The underlying Microsoft Graph operation is `POST /communications/calls` using `appHostedMediaConfig` when you want raw media.

See Microsoft’s example “Join scheduled meeting with application hosted media” in the create call doc.  
**Source:** S6.

## D11. Live transcription: Teams audio frames → Azure Speech → Python

### D11.1 Confirm the audio format you will receive from Teams
Microsoft’s real-time media concepts doc specifies audio characteristics such as:
- Audio frames are 20 ms each
- Audio is 16,000 samples/sec
- 16-bit depth
- A 20 ms buffer contains 320 samples and therefore 640 bytes (at 16 bits per sample)

**Source:** S5.

### D11.2 Create an Azure Speech resource and get key/region
1. In Azure Portal, create an Azure AI Speech resource (Speech service).
2. Copy the Speech key and region.

(Portal UI steps vary over time, but the Speech docs explain how to use the keys/region with SDKs.)  
**Source:** S18, S15.

### D11.3 Add the Speech SDK package to your bot project
In your bot’s `.csproj` add (or via NuGet UI):

```xml
<PackageReference Include="Microsoft.CognitiveServices.Speech" Version="*" />
```

*(Use the latest stable version compatible with your project.)*

**Source:** Speech SDK usage patterns in S15/S18.

### D11.4 Add a streaming transcriber class (copy/paste from Part B)
This guide already provides a minimal `AzureSpeechRealtimeTranscriber` class in **B2**.

Key correctness points (all documented):
- Speech SDK supports **streaming input** via audio input streams.  
  Source: S15.
- Supported raw audio format includes 16-bit PCM, 16 kHz, mono, little-endian.  
  Source: S15.
- `PushAudioInputStream` defaults to 16 kHz / 16-bit / mono PCM (so it matches Teams audio).  
  Source: S16.

### D11.5 Wire your sample’s audio callback to the transcriber
Your app-hosted media sample will have an audio receive event (commonly something like `AudioMediaReceived`). In that handler:

1. Extract the raw PCM bytes for each 20 ms frame
2. Call `_stt.PushPcm16k16bitMono(frameBytes)`

Pseudo-pattern (adapt to the exact buffer type in your sample):

```csharp
// 1) Start the transcriber when the call connects
await _stt.StartAsync();

// 2) On each audio frame from Teams
void OnAudioFrame(byte[] pcmFrame20ms)
{
    _stt.PushPcm16k16bitMono(pcmFrame20ms);
}
```

**Sources:** S5 (frame-by-frame real-time media), S15–S16 (streaming STT input).

### D11.6 Send transcription events to Python in real time
The provided transcriber class posts JSON to a Python endpoint (FastAPI) for immediate agent consumption.

**Source:** This is an implementation choice; the transcriber’s HTTP POST is standard and the Speech events are documented in S18.

## D12. Python transcript receiver (FastAPI) + agent hook

### D12.1 Create a Python virtual environment (recommended)
```bash
python -m venv .venv
source .venv/bin/activate  # (macOS/Linux)
# or on Windows PowerShell:
.\.venv\Scripts\Activate.ps1
```

### D12.2 Install deps
```bash
pip install fastapi uvicorn
```

### D12.3 Minimal receiver (prints JSON, then calls your agent)
Create `transcript_sink.py`:

```python
from fastapi import FastAPI, Request
import uvicorn

app = FastAPI()

@app.post("/transcript")
async def transcript(req: Request):
    payload = await req.json()
    # Example payload: {"kind":"recognized", "text":"...", "tsUtc":"..."}
    print(payload)
    # TODO: call into your Python agent framework here
    return {"ok": True}

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765)
```

Run it:

```bash
python transcript_sink.py
```

### D12.4 Wire to your agent framework (example stub)
Replace the `print(payload)` with something like:

```python
async def transcript(req: Request):
    payload = await req.json()
    text = payload.get("text")
    if text:
        # agent.consume(text)  # <- your framework here
        pass
    return {"ok": True}
```

## D13. End-to-end test procedure (from zero → transcript)

Follow this exact order:

### D13.1 Start your Python transcript sink
- Run `python transcript_sink.py` and leave it running.

### D13.2 Start ngrok (both tunnels)
- Run `ngrok start --all --config C:\ngrok\ngrok.yml`.
- Copy the HTTPS URL and the TCP public port.

**Source:** S2.

### D13.3 Update your Azure Bot “Webhook (for calling)” if ngrok HTTPS changed
Because free ngrok domains change, you may need to paste the new HTTPS URL each run.

**Source:** S1 (calling webhook is required).

### D13.4 Run the bot locally (Visual Studio)
- Start debugging / run the bot project.

### D13.5 Create a Teams meeting and copy the join link
- Any meeting works for POC; copy the join URL.

### D13.6 Tell the bot to join the meeting
- Call the sample’s join endpoint via ngrok HTTPS (see D10.2).

### D13.7 Validate you are receiving audio + transcripts
Success looks like:
- Bot logs show call connected / media session established
- Your Python console prints JSON events like:

```json
{"kind":"recognized","text":"hello everyone","tsUtc":"2026-01-28T20:33:12.3456789Z"}
```

**Sources:** S5 (real-time media processing), S18 (Speech recognized events).


---

# PART E — Minimal deployment options (still POC-focused)

> You said “fastest possible to functionality.” For most teams, **local + ngrok** is fastest. This section only exists because app-hosted media bots cannot be deployed to Azure Web Apps, so the next simplest place is a Windows VM.

**Sources:** S4 (Windows requirement; Azure Web App limitation).

## E1. Option 1 (fastest): Run on a Windows dev box + ngrok
That’s what Parts A–D implement.

## E2. Option 2: Run on an Azure Windows VM (no ngrok, stable endpoints)

### E2.1 When you should do this
- You want a stable public DNS name and don’t want to keep updating ngrok URLs.
- You want the bot always-on for demos.

*(Still POC—no scaling.)*

### E2.2 Create a Windows VM
1. Azure Portal → Virtual Machines → Create (Windows Server 2022 is a common choice).
2. Assign a public IP.
3. Open inbound ports:
   - 443 (HTTPS signaling)
   - Your media port (for example 8445, or another port you choose)

### E2.3 Point DNS to the VM instead of ngrok
- `bot.yourdomain.com` → A record to the VM public IP (signaling)
- `media.yourdomain.com` → A record to the VM public IP (media)

### E2.4 Install a public certificate on the VM
- Use the same wildcard cert approach.
- Configure your bot to use `ServiceFqdn = media.yourdomain.com` and `InstancePublicPort = 8445`.

### E2.5 Update Azure Bot calling webhook to the VM’s HTTPS endpoint
Example:

```text
https://bot.yourdomain.com/api/calling
```

### E2.6 Run the bot on the VM
- Same bot binaries, just hosted on Windows VM.

**Sources:** S4 (Windows), S2/S11 (ServiceFqdn + ports + cert requirement), S1 (calling webhook).


---

# PART F — Troubleshooting (functional blockers only)

## F1. “Upload a custom app” is missing in Teams
**Likely cause:** Custom app upload is disabled by Teams admin policy.

**Fix:** Enable custom app upload / app permission policy so you can sideload the app package internally.

**Source:** S21.

## F2. Bot joins call but you get no media / audio frames
Work through these in order:

1. Confirm you are using **application-hosted media** (not service-hosted).  
   Source: S4, S14.
2. Confirm your bot is running on **Windows** and using a recent media library version.  
   Source: S4.
3. Confirm your **ngrok TCP tunnel** is running and points to the bot’s media port (8445).  
   Source: S2.
4. Confirm `ServiceFqdn` matches your **DNS name you control** (like `0.botpoc.example.com`) and NOT the raw ngrok host.  
   Source: S2.
5. Confirm the certificate thumbprint matches a certificate whose subject/SAN covers the `ServiceFqdn`.  
   Source: S2.

## F3. Media connection fails / TLS errors
**Most common causes:**
- ServiceFqdn does not match cert subject/SAN
- You used a self-signed cert (not publicly trusted)
- DNS CNAME does not point to `0.tcp.ngrok.io`

**Fix:** Re-do D3 + D4 carefully; these are hard requirements.

**Source:** S2.

## F4. You get error `7504` / “Insufficient enterprise tenant permissions”
**Meaning:** Your tenant is not provisioned/enabled for these APIs (preview capability).

**Fix:** You must open a Microsoft support request and ask for Cloud Communications calling enablement for your tenant.

**Sources:** S20, S4.

## F5. Speech recognizer never produces text
Work through these:

1. Confirm you are actually receiving audio frames from Teams (log frame sizes / counts).  
   Source: S5.
2. Confirm you are feeding the recognizer PCM frames in the expected format (16 kHz, 16-bit, mono).  
   Sources: S5, S15, S16.
3. Confirm you started continuous recognition (`StartContinuousRecognitionAsync`) and did not dispose early.  
   Sources: S17, S18.
4. Temporarily log Speech SDK `Canceled` events to see if you have auth/region issues.  
   Source: S18.

## F6. You want to save transcripts / recordings
Reminder: Microsoft’s bot media guidance says you can’t “record or otherwise persist” media content (or derived data) unless you follow the policy recording flow and call `updateRecordingStatus`.

**Sources:** S8, S9, S1.


---

# PART G — Extremely explicit portal click paths (for admins who want “no ambiguity”)

> This section is optional. It exists because teams often lose time to portal navigation.
>
> Where portal UI labels change over time, the *resource names* are stable (App registrations, API permissions, Azure Bot, Teams channel).

**Sources:** S1, S13, S15, S21.

## G1. Entra app registration (portal click path)
1. Azure Portal → search **Microsoft Entra ID** → open it.
2. Left nav → **App registrations**.
3. Top menu → **New registration**.
4. Fill:
   - Name: `TeamsMediaBotPOC`
   - Supported account types: *Single tenant*
5. Click **Register**.
6. On the Overview page, copy:
   - Application (client) ID
   - Directory (tenant) ID

**Source:** S13.

## G2. Create client secret (portal click path)
1. In the app registration, left nav → **Certificates & secrets**.
2. Under Client secrets → **New client secret**.
3. Description: `dev`
4. Expires: pick something long enough for the POC window.
5. Click **Add**.
6. Copy the **Value** immediately (you won’t see it again).

**Source:** S13.

## G3. Add Graph permissions + admin consent (portal click path)
1. App registration → left nav → **API permissions**.
2. Click **Add a permission**.
3. Choose **Microsoft Graph**.
4. Choose **Application permissions**.
5. Add:
   - `Calls.AccessMedia.All`
   - `Calls.JoinGroupCall.All`
6. Back on the API permissions page, click **Grant admin consent**.

**Sources:** S1, S6.

## G4. Azure Bot resource (portal click path)
1. Azure Portal → Create resource.
2. Search `Azure Bot` → select it → Create.
3. In the creation wizard, select **Use existing app registration** (paste your App (client) ID).
4. Create the bot.

**Source:** S1.

## G5. Add Microsoft Teams channel + enable Calling (portal click path)
1. Open your Azure Bot resource.
2. Left nav → **Channels**.
3. Add **Microsoft Teams** channel.
4. Open the Teams channel configuration.
5. Find **Calling** (tab/section).
6. Enable calling and set the webhook to:
   `https://<YOUR_NGROK_SUBDOMAIN>.ngrok-free.app/api/calling`

**Sources:** S1, S2.

## G6. Create Azure Speech resource (portal click path)
1. Azure Portal → Create resource.
2. Search `Speech` or `Speech service`.
3. Create an Azure AI Speech resource.
4. After creation, open the resource → Keys and Endpoint.
5. Copy:
   - Key 1
   - Region

**Sources:** S15, S18.

## G7. Teams: upload your custom app (end-user UI path)
1. Open Teams (desktop client is easiest for dev).
2. Left nav → **Apps**.
3. Click **Upload a custom app**.
4. Select your Teams app zip (manifest + icons).
5. Click **Add**.

**Source:** S21.


---

# PART H — Copy/paste templates (edit placeholders, then map to your sample)

> These templates are **not** tied to one specific sample. Different Microsoft samples use different config shapes.
> The goal is to give you concrete, copy/paste-able starting points.

**Sources:** S2 (ngrok + ports), S11/S14 (MediaPlatformSettings fields), S15–S18 (Speech), S1 (Teams manifest bot fields).

## H1. `ngrok.yml` (repeat of D5.2 for convenience)
```yaml
version: "2"
authtoken: <YOUR_NGROK_AUTHTOKEN>

tunnels:
  signaling:
    proto: http
    addr: 9441
  media:
    proto: tcp
    addr: 8445
```

## H2. `appsettings.poc.json` (example shape you can adapt)

```json
{
  "Bot": {
    "TenantId": "<TENANT_ID>",
    "AppId": "<APP_CLIENT_ID>",
    "AppSecret": "<APP_CLIENT_SECRET>",
    "NotificationUrl": "https://<NGROK_SUBDOMAIN>.ngrok-free.app/api/calling",
    "LocalHttpListenUrl": "http://0.0.0.0:9441"
  },

  "MediaPlatformSettings": {
    "ApplicationId": "<APP_CLIENT_ID>",
    "CertificateThumbprint": "<MEDIA_CERT_THUMBPRINT>",
    "InstanceInternalPort": 8445,
    "InstancePublicPort": 12345,
    "ServiceFqdn": "0.botpoc.example.com",
    "InstancePublicIPAddress": "0.0.0.0"
  },

  "Speech": {
    "Key": "<SPEECH_KEY>",
    "Region": "<SPEECH_REGION>",
    "RecognitionLanguage": "en-US"
  },

  "TranscriptSink": {
    "PythonEndpoint": "http://127.0.0.1:8765/transcript"
  }
}
```

## H3. Graph token acquisition (client credentials) — Postman-style values

To call Graph as the app (not as a user), you need a client credentials token:

```text
Token URL: https://login.microsoftonline.com/<TENANT_ID>/oauth2/v2.0/token
Client ID: <APP_CLIENT_ID>
Client Secret: <APP_CLIENT_SECRET>
Scope: https://graph.microsoft.com/.default
Grant Type: client_credentials
```

**Source:** This is standard Entra OAuth2 client credentials flow; the Graph docs use this model for application permissions. (See S13 for the app registration prerequisite.)

## H4. Graph “create call” endpoint (for debugging)

Endpoint:

```text
POST https://graph.microsoft.com/v1.0/communications/calls
```

See S6 for the correct request bodies (including the app-hosted media meeting join example).  
**Source:** S6.


---

# PART I — Drop-in code modules (C# + Python) for the “audio → transcript → agent” pipe

> If you already have a bot that receives audio frames, you can copy/paste these files and be done.
> These are intentionally small and do not try to abstract anything.

**Sources:** S5 (frame format), S15–S18 (Speech streaming + events).

## I1. C# — `TranscriptEvent` model
```csharp
public record TranscriptEvent(
    string Kind,   // "recognizing" | "recognized" | "session_started" | "session_stopped" | "canceled"
    string? Text,
    string TsUtc,
    string? Details = null
);
```

## I2. C# — `PythonTranscriptPublisher` (HTTP POST)
```csharp
using System.Net.Http.Json;

public sealed class PythonTranscriptPublisher : IDisposable
{
    private readonly HttpClient _http = new();
    private readonly Uri _endpoint;

    public PythonTranscriptPublisher(string endpointUrl)
    {
        _endpoint = new Uri(endpointUrl);
    }

    public Task PublishAsync(TranscriptEvent evt, CancellationToken ct = default)
        => _http.PostAsJsonAsync(_endpoint, evt, ct);

    public void Dispose() => _http.Dispose();
}
```

## I3. C# — `AzureSpeechRealtimeTranscriber` (streaming PushAudioInputStream)
> This is a slightly more “event-safe” version of the Part B code: it keeps the event handlers synchronous and pushes publishing onto the thread pool.

```csharp
using System;
using System.Threading;
using System.Threading.Tasks;
using Microsoft.CognitiveServices.Speech;
using Microsoft.CognitiveServices.Speech.Audio;

public sealed class AzureSpeechRealtimeTranscriber : IAsyncDisposable
{
    private readonly string _speechKey;
    private readonly string _speechRegion;
    private readonly string _lang;
    private readonly PythonTranscriptPublisher _publisher;

    private PushAudioInputStream? _push;
    private SpeechRecognizer? _recognizer;

    public AzureSpeechRealtimeTranscriber(
        string speechKey,
        string speechRegion,
        string language,
        PythonTranscriptPublisher publisher)
    {
        _speechKey = speechKey;
        _speechRegion = speechRegion;
        _lang = language;
        _publisher = publisher;
    }

    public async Task StartAsync(CancellationToken ct = default)
    {
        if (_recognizer != null) return;

        // Teams audio is already 16 kHz / 16-bit / mono PCM per real-time media docs,
        // and PushAudioInputStream defaults to 16 kHz / 16-bit / mono PCM.
        // Sources: S5, S16.
        _push = AudioInputStream.CreatePushStream();
        var audio = AudioConfig.FromStreamInput(_push);

        var cfg = SpeechConfig.FromSubscription(_speechKey, _speechRegion);
        cfg.SpeechRecognitionLanguage = _lang;

        _recognizer = new SpeechRecognizer(cfg, audio);

        _recognizer.SessionStarted += (_, __) => FireAndForget("session_started", null);
        _recognizer.SessionStopped += (_, __) => FireAndForget("session_stopped", null);
        _recognizer.Recognizing += (_, e) => FireAndForget("recognizing", e.Result?.Text);
        _recognizer.Recognized += (_, e) => FireAndForget("recognized", e.Result?.Text);
        _recognizer.Canceled += (_, e) => FireAndForget("canceled", null, e.ErrorDetails);

        await _recognizer.StartContinuousRecognitionAsync().ConfigureAwait(false);
    }

    public void PushPcm16k16bitMono(ReadOnlySpan<byte> pcmFrame)
    {
        // Audio frames are typically 20 ms = 640 bytes, but don’t hardcode; just write what you receive.
        // Source: S5.
        _push?.Write(pcmFrame.ToArray());
    }

    public async Task StopAsync()
    {
        if (_recognizer == null) return;
        await _recognizer.StopContinuousRecognitionAsync().ConfigureAwait(false);
        _recognizer.Dispose();
        _recognizer = null;

        _push?.Close();
        _push = null;
    }

    private void FireAndForget(string kind, string? text, string? details = null)
    {
        var evt = new TranscriptEvent(kind, text, DateTime.UtcNow.ToString("O"), details);
        _ = Task.Run(() => _publisher.PublishAsync(evt));
    }

    public async ValueTask DisposeAsync()
    {
        await StopAsync().ConfigureAwait(false);
    }
}
```

## I4. Python — Receiver with an async queue (agent-friendly)
```python
from fastapi import FastAPI, Request
import uvicorn
import asyncio

app = FastAPI()
queue: asyncio.Queue[dict] = asyncio.Queue()

@app.post("/transcript")
async def transcript(req: Request):
    payload = await req.json()
    await queue.put(payload)
    return {"ok": True}

async def agent_loop():
    while True:
        evt = await queue.get()
        text = evt.get("Text") or evt.get("text")  # depending on your C# JSON casing
        kind = evt.get("Kind") or evt.get("kind")
        if kind == "recognized" and text:
            # TODO: call your agent framework here
            print(f"AGENT_INPUT: {text}")

if __name__ == "__main__":
    loop = asyncio.get_event_loop()
    loop.create_task(agent_loop())
    uvicorn.run(app, host="127.0.0.1", port=8765)
```


---

# PART J — Wiring the transcriber into a Microsoft Graph communications sample (search-driven, no guessing)

Because Microsoft samples evolve, the only reliable approach is to **search for stable concepts** rather than hardcoding file names.

**Sources:** S14 (app-hosted media call patterns), S10 (media SDK concepts).

## J1. Locate where audio frames arrive
Search for these strings in the sample solution:

```text
AudioMediaReceived
AudioSocket
GetLocalMediaSession
IAudioSocket
```

In many samples, you will see something like (pattern only):

```csharp
call.GetLocalMediaSession().AudioSocket.AudioMediaReceived += OnAudioReceived;
```

**Sources:** S10, S14.

## J2. Start the transcriber when the call is connected and media session exists
You need to start Speech recognition **after** you have a media session.

Search for call state transitions:

```text
CallState.Established
CallState.Connected
OnCallStateChanged
StateChanged
```

Then add:

```csharp
await _stt.StartAsync();
```

…once per call.

**Sources:** Speech continuous recognition lifecycle is described in S18; app-hosted media lifecycle is described in S14.

## J3. Feed each audio frame into `_stt.PushPcm16k16bitMono(...)`
In your audio receive handler:

1. Extract the `byte[]` / span for PCM audio.
2. Call:

```csharp
_stt.PushPcm16k16bitMono(pcmFrameBytes);
```

**Sources:** S5 (PCM frame format), S15–S16 (push stream).

## J4. Stop the transcriber when the call ends
Search for termination paths:

```text
CallState.Terminated
Dispose
Hangup
```

Then call:

```csharp
await _stt.StopAsync();
```

**Source:** Speech recognizer lifecycle in S18.


---

# PART K — MediaPlatformSettings cheat sheet (so you don’t mis-map ports/domains)

These fields show up in Microsoft’s docs and samples for application-hosted media.

**Sources:** S2, S11, S14.

## K1. `ServiceFqdn`
- **What it is:** The DNS name Teams/Graph will connect to for your media endpoint.
- **What it must be in local dev:** A hostname you control (for example `0.botpoc.example.com`) that is a **CNAME** to `0.tcp.ngrok.io`.
- **What it must match:** Your media certificate subject/SAN (wildcard is easiest).

**Source:** S2.

## K2. `InstancePublicPort`
- **What it is:** The TCP port on the public side (what Teams/Graph will dial).
- **In ngrok dev:** The random port ngrok prints for the TCP tunnel (example `12345` in `tcp://0.tcp.ngrok.io:12345`).

**Sources:** S2, S11.

## K3. `InstanceInternalPort`
- **What it is:** The local TCP port your bot process is listening on for media.
- **In this guide:** 8445.

**Sources:** S2, S11.

## K4. `CertificateThumbprint`
- **What it is:** The thumbprint of the cert your bot uses to establish media connections.
- **Rule:** Must correspond to a publicly trusted certificate matching `ServiceFqdn`.

**Source:** S2.

## K5. `NotificationUrl` / callback URL (signaling)
- **Rule:** Must be HTTPS and reachable publicly (ngrok HTTPS is fine).
- **Local listener:** Can be HTTP when ngrok terminates TLS (no E2E encryption).

**Sources:** S2, S11.


---

# PART L — “Green check” validation checklist (stop at the first red X)

Use this to avoid debugging the wrong layer.

**Sources:** S1, S2, S4, S5, S11, S15–S18, S20, S21.

## L1. Tenant / API enablement
- [ ] Creating/joining a call does **not** fail with `7504` / tenant permission errors. (If it does, stop and file support request.)  
      Source: S20.

## L2. Teams client / sideloading
- [ ] You can see **Upload a custom app** in Teams.  
      Source: S21.
- [ ] Your Teams app installs successfully and the bot appears.  
      Source: S1.

## L3. ngrok
- [ ] ngrok shows an **HTTPS** tunnel for signaling.  
      Source: S2.
- [ ] ngrok shows a **TCP** tunnel for media (`0.tcp.ngrok.io:<port>`).  
      Source: S2.

## L4. DNS
- [ ] `nslookup 0.botpoc.example.com` resolves and is a CNAME to `0.tcp.ngrok.io`.  
      Source: S2.

## L5. Certificate
- [ ] A public-trusted wildcard cert covering `*.botpoc.example.com` is installed in `LocalMachine\My`.
- [ ] You copied the correct thumbprint.

      Source: S2.

## L6. Bot config
- [ ] `ServiceFqdn = 0.botpoc.example.com` (NOT `0.tcp.ngrok.io`).  
      Source: S2.
- [ ] `InstancePublicPort` equals ngrok’s TCP port.
- [ ] `InstanceInternalPort` equals your local media port (8445).

      Sources: S2, S11.

## L7. Media
- [ ] Bot logs show call established and media session created.
- [ ] Your audio receive handler is invoked repeatedly (~50 times/sec).  
      Source: S5.

## L8. Speech
- [ ] You started continuous recognition and registered event handlers.  
      Source: S18.
- [ ] You are pushing PCM frames into a PushAudioInputStream (streaming input).  
      Sources: S15, S16.
- [ ] You see `recognized` events with text.

## L9. Python
- [ ] FastAPI endpoint receives JSON events in real time.


---

# PART M — FAQ (functional only, no governance/scaling)

## M1. Can I run this on Linux or macOS?
No. Application-hosted media bots require a **Windows** environment.

**Source:** S4.

## M2. Can I deploy this to Azure App Service / Web App?
No. Microsoft explicitly notes app-hosted media bots cannot be deployed to Azure Web Apps.

**Source:** S4.

## M3. Can I use a self-signed certificate to move faster?
Not for the app-hosted media endpoint validation described in Microsoft’s local testing guidance. Use a publicly trusted cert matching your own DNS name (wildcard recommended).

**Source:** S2.

## M4. Can I make the certificate for `*.ngrok-free.app` or `*.tcp.ngrok.io`?
No. Those domains are not yours to obtain certificates for. You must use a domain you control (CNAME to ngrok’s fixed TCP hosts).

**Source:** S2.

## M5. Do I need to publish anything to the Teams Store?
No. For internal POC, use **custom app upload** (sideload).

**Source:** S21.

## M6. Why must the bot callback/notification URL be HTTPS?
Microsoft’s local testing guidance states the notification URL must be HTTPS; ngrok provides that while your local listener can remain HTTP (when E2E encryption isn’t used).

**Source:** S2.

## M7. Why is audio frame size mentioned everywhere?
Because the media stack delivers real-time audio in fixed time slices (20 ms frames). This is the unit you push into streaming speech recognition.

**Source:** S5.

## M8. Do I have to call `updateRecordingStatus`?
If you are doing policy-based recording or persisting media/derived data in a way that triggers recording requirements, Microsoft’s Graph docs point to `updateRecordingStatus`. For the pure “live transcript to agent memory” POC, you still should understand this constraint.

**Sources:** S1, S8, S9.

## M9. Why is there a tenant provisioning step?
Some tenants encounter hard “insufficient enterprise tenant permissions” errors (for example error `7504`) and need Microsoft to provision/enable the capability.

**Source:** S20.


---

# PART N — Validation metadata + change log

- **Guide validation date:** 2026-01-28 (America/Los_Angeles).
- **Docs used:** Microsoft Learn + official Microsoft repos listed in Sources index S1–S22.
- **Non-goals (intentionally excluded):** scalability, multi-tenant governance, production hardening, store publishing.

## N1. Key functional assumptions
- You have a Microsoft 365 tenant and are a Global Admin (or can grant admin consent).
- You can run the bot on a Windows machine (local dev or Windows VM).
- You can register/manage DNS for a domain you control.

## N2. Known moving parts you should re-check if something breaks
- Preview enablement / tenant provisioning (S4, S20).
- Teams app manifest schema versions (S1).
- ngrok hostnames and behavior (S2).


# Mobile Apps

Editions: OSS, Cloud, Enterprise. Unless stated otherwise, everything on this page ships in OSS.

Preloop mobile apps let approvers review requests and respond without opening a laptop. They are the fastest way to keep agents moving while keeping humans in control.

<div class="grid cards" markdown>

-   :material-apple: **iOS**

    ---

    iPhone, iPad, and Apple Watch.

    [Open on the App Store](https://apps.apple.com/us/app/preloop/id6757803021)

-   :material-android: **Android**

    ---

    Android phones and tablets.

    [Open on Google Play](https://play.google.com/store/apps/details?id=ai.spacecode.preloop)

</div>

---

## What You Can Do

From the mobile apps, approvers can:

- receive approval notifications
- open the request with full context
- approve or decline the action
- add a short response message back to the agent

The current mobile apps are production approval clients. Agent Control voice and dictation surfaces are scaffolded so phone and watch actions can become audited operator messages to enrolled agents such as OpenClaw and Hermes, but live delivery depends on the backend Agent Control endpoint and the agent runtime's native plugin being loaded and online. The mobile app sends commands to Preloop; it does not own the agent's long-lived control WebSocket, reconnect loop, heartbeat/status reporting, capability advertisement, or command execution.

<figure>
  <video autoplay muted loop playsinline style="width: 100%; border-radius: 8px;">
    <source src="../../../assets/animations/quickstart/mobile_approval.mp4" type="video/mp4">
  </video>
  <figcaption>Approving a request from the mobile app</figcaption>
</figure>

<figure>
  <img src="../../../assets/screenshots/quickstart/mobile_approval.png" alt="Approval request in the Preloop mobile app" style="max-width: 360px; width: 100%; border-radius: 18px; box-shadow: 0 4px 12px rgba(0,0,0,0.15);">
  <figcaption>A real approval request shown in the mobile app.</figcaption>
</figure>

---

## iOS App

The iOS app supports **iPhone**, **iPad**, and **Apple Watch**.

### Install

1. Download **Preloop** from the [App Store](https://apps.apple.com/us/app/preloop/id6757803021)
2. Open the app and sign in with your Preloop account
3. Allow notifications when prompted
4. If you use a self-hosted deployment, enter your server URL before signing in

<figure>
  <video autoplay muted loop playsinline style="width: 100%; border-radius: 8px;">
    <source src="../../../assets/animations/quickstart/mobile_login.mp4" type="video/mp4">
  </video>
  <figcaption>Signing in to the iOS app</figcaption>
</figure>

!!! note "Apple Watch"
    The watch app is available alongside the iPhone app and is designed for fast approve/decline decisions when you are away from your desk.

!!! info "Voice control status"
    Voice contact is an Agent Control path, not a separate background voice transport. Native push-to-talk and watch dictation can use vendor STT/TTS, then send the transcript to Preloop as an audited operator message. Siri Shortcuts and App Intents are useful for launching narrow actions or handing a confirmed command into the app, but they should not be treated as an always-open background agent chat channel.

---

## Android App

The Android app is available on [Google Play](https://play.google.com/store/apps/details?id=ai.spacecode.preloop).

### Install

1. Download **Preloop** from [Google Play](https://play.google.com/store/apps/details?id=ai.spacecode.preloop)
2. Open the app and sign in with your Preloop account
3. Allow notifications when prompted
4. If you use a self-hosted deployment, enter your server URL before signing in

The core approval experience matches iOS: receive notifications, inspect the request, and approve or decline directly from your phone.

!!! info "Google Assistant status"
    Android voice contact is an Agent Control path, not a general Assistant transport. Native push-to-talk can use vendor STT/TTS, with Google Assistant/App Actions limited to invocation or deep-link handoff for narrow flows. Preloop keeps command state, policy checks, and audit history on the server.

---

## Remote sessions on your runners (coming)

Starting a session with an agent on one of your own machines (for example
GitHub Copilot CLI behind a Preloop runner) works from the web console today
and is coming to the mobile apps. The apps will use the same endpoints as the
console:

- `GET /api/v1/runners/{runner_id}/session-options`
- `POST /api/v1/runners/{runner_id}/sessions`
- `GET /api/v1/runners/{runner_id}/sessions`
- `POST /api/v1/runner-sessions/{session_id}/turns`
- `POST /api/v1/runner-sessions/{session_id}/stop`

See [Remote sessions on your runners](../runners/remote-sessions.md) for the
request shapes, who may start a session and why a start can be refused.

---

## Self-Hosted Deployments

Both mobile apps work with self-hosted Preloop deployments.

To connect a self-hosted environment:

1. Open the app
2. Enter your server URL
3. Sign in with your normal Preloop credentials

For the best experience, use an HTTPS endpoint with a valid certificate so notifications and app connectivity work reliably.

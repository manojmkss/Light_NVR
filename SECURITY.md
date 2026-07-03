# Security Policy

LightNVR handles camera credentials, recorded video, and (optionally) remote
access into your home network. Vulnerabilities here have real consequences,
so please report them privately rather than through a public issue.

## Reporting a vulnerability

**Preferred:** use GitHub's private vulnerability reporting for this repo
(Security tab -> "Report a vulnerability"). It goes only to the maintainer
and creates a private discussion thread to work out a fix before anything is
public.

**Fallback:** email manojmkss@gmail.com with a description of the issue,
steps to reproduce, and the affected version/commit. Please don't include
exploit details in a public issue, PR, or discussion.

This is a small, self-hosted project maintained on a best-effort basis -
there's no formal SLA, but security reports get priority over everything
else and an acknowledgment within a few days is the goal.

## Scope

In scope: anything in this repository - the FastAPI backend, the React
frontend, the nginx/Docker configuration, and the Tailscale/Cloudflare
integrations as wired up here.

Out of scope: vulnerabilities in upstream dependencies (FFmpeg, OpenCV,
onvif-zeep-async, etc.) - please report those to the upstream project
directly, though a heads-up here is still welcome so this project can pin
around them.

Particularly interested in reports involving:
- Authentication/authorization bypass (JWT handling, role checks, kiosk
  token scoping)
- Credential exposure (camera RTSP/ONVIF passwords, SMTP/Telegram/WhatsApp
  tokens, TLS private keys)
- Path traversal or injection in recording export, backup restore, or
  clip-export filename handling
- SSRF via camera stream URLs or ONVIF discovery
- Anything that lets an unauthenticated caller reach a camera feed or
  recording they shouldn't

## Supported versions

Only the latest commit on `main` is supported. There's no long-term-support
branch at this project's current size - upgrading is the fix for any known
issue.

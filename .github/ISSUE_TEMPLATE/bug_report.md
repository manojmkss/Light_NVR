---
name: Bug report
about: Something isn't working as expected
labels: bug
---

**What happened**
A clear description of the bug.

**What you expected**
What should have happened instead.

**Steps to reproduce**
1.
2.
3.

**Environment**
- Deployment: Docker / bare-metal, OS + architecture (e.g. Ubuntu 22.04 x86_64, Raspberry Pi OS arm64)
- Camera make/model + protocol (ONVIF / manual RTSP), if camera-related
- Browser, if a frontend issue

**Logs**
Relevant output from `docker compose logs backend` (redact hostnames/credentials
if the log line includes an RTSP URL - camera URLs often embed a password).

**Screenshots**
If applicable.

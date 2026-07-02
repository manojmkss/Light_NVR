from pydantic import BaseModel


class RemoteAccessSettingsOut(BaseModel):
    tailscale_enabled: bool
    tailscale_hostname: str
    has_tailscale_authkey: bool

    cloudflare_enabled: bool
    has_cloudflare_token: bool


class TailscaleUpdate(BaseModel):
    enabled: bool
    authkey: str | None = None  # leave blank to keep the current key
    hostname: str | None = None


class CloudflareUpdate(BaseModel):
    enabled: bool
    token: str | None = None  # leave blank to keep the current token


class TailscaleStatusOut(BaseModel):
    state: str
    ip: str | None
    hostname: str | None
    error: str | None


class CloudflareStatusOut(BaseModel):
    state: str
    error: str | None
    log_tail: list[str]


class RemoteAccessStatusOut(BaseModel):
    tailscale: TailscaleStatusOut
    cloudflare: CloudflareStatusOut

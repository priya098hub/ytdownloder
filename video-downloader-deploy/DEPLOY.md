# Deployment package

This archive contains the runnable Python backend, frontend, tests, Windows launcher, and a Dockerfile. It is not an HTML-only site. Uploading index.html alone will not run the downloader.

## Run the container

Requires Docker with Linux containers. From the extracted project directory:

```sh
docker build -t video-downloader .
docker run --rm -p 127.0.0.1:8000:8000 video-downloader
```

Open http://127.0.0.1:8000 for a local container test.

## Configure an HTTPS deployment

Use infrastructure capable of running this Dockerfile, outbound media downloads, background subprocesses, and temporary disk storage. This package does not select a hosting provider or guarantee a free plan.

1. Upload the extracted source to your own private repository or your host's container build context. Do not upload .venv or local temporary media.
2. Build using the root Dockerfile. Set the service port to 8000, or set PORT to the port your host assigns.
3. Configure TLS/HTTPS at the host or reverse proxy and preserve the original Host header.
4. Set PUBLIC_ORIGIN to the exact HTTPS origin of your app, for example https://video.example.com (no path). Leaving it unset keeps requests restricted to localhost hosts. Wildcards are intentionally not supported.
5. Use exactly one worker and one replica: the job registry is in memory. Multiple replicas would need shared job storage, a queue, and shared file storage, which this package does not implement.
6. Use /api/health for the health check. Health requests must carry the configured public Host or a localhost Host.
7. Keep server-side request timeouts above 110 seconds for analysis; browser polling tracks background downloads separately. Completed files expire and are temporary, not permanent storage.

The container runs as a non-root user. Python, Node, FFmpeg, and FFprobe are included. Public-origin validation remains exact and rejects cross-origin API requests; this is not authentication or protection against automated direct requests.

## Before allowing unrestricted public use

This is a deployment starter, not a production-scale anonymous service. Put authentication or an access gateway and per-user/IP rate limits in front of it; add traffic/resource quotas, abuse monitoring, suitable privacy/usage policies, and confirm permission from the content source and host. Otherwise public requests can consume your bandwidth and CPU even with the single-download limit. Do not expose an unrestricted endpoint based only on CORS/Origin checks.

No private-content access, cookies, DRM bypass, or paid-content bypass is included. Only process content you are authorized to save and where the source permits it. Hosting/account acceptance and media availability are not guaranteed.

## Rewarded ads

No ad SDK, ad account, or simulated rewarded success is included. Integration requires a specific provider, publisher approval, real placement IDs, privacy/consent requirements, and confirmation that the provider accepts the site's actual content. Never treat closing an ad as proof of reward; use the provider's documented completion/verification flow. This archive does not claim ad monetization approval.

## Ad placement hooks

The page has empty `data-ad-slot` containers for `banner`, `native`, and `social-bar` placements, plus a comment at the end of `static/index.html` marking where to add a provider-approved popunder tag. Empty containers are hidden, so the existing page layout is unchanged until an ad provider adds content. No ad scripts are loaded by default.

The Content Security Policy in `app/main.py` intentionally permits only same-origin scripts and connections. Before adding a provider, review its publisher and privacy requirements, then allow only its required origins in the policy; do not broadly enable arbitrary scripts. Ad providers and their behavior are not tested or included in this package.

## Verification status

Application tests can be run with requirements-dev.txt and pytest. The Dockerfile is supplied but has not been built in this environment. No public deployment has been performed. The earlier README describes local operation; this file documents the optional PUBLIC_ORIGIN deployment configuration.

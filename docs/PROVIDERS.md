# Providers

Facts below were read from provider documentation on **2026-10-28**. The
Providers screen in the app is the live copy: prices, limits, and whether
the request shape was confirmed. An adapter marked unverified has never
returned a real video to this app. Do not treat this table as proof that a
call will succeed.

## APIs

| Provider | Free API allowance | Endpoint confirmed from docs | Notes |
| --- | --- | --- | --- |
| Alibaba Model Studio (Wan) | 50s per i2v model, once, 90 days, Singapore | No — submit path still needs a check | Only recurring-looking grant is actually one-off |
| BytePlus ModelArk (Seedance) | 2,000,000 tokens on each of two models, once | No | Billed in tokens; USD in the app is estimated |
| fal.ai | None for video | Yes | Paid |
| Replicate | None found | No | Paid; reports actual cost on the prediction |
| MiniMax (Hailuo) | None for the API | Yes | Website credits are a different wallet |
| PixVerse API | None shared with the website | No — host was not confirmed | Docs say API credits are not website credits |
| Runway | None; credits are purchased | Yes | No idempotency key; a lost submit is parked |
| Luma | None found | Yes | 5s and 10s are different prices, not a per-second rate |
| Pika | None found | Yes | Image is uploaded first; that step is safe to retry |
| Vidu | None for the API | Yes | USD shown is converted from CNY and marked estimated |
| Kling | None shared with the website | Yes | Signed JWT; the secret is not sent as a header |

There is no image-to-video API in this set with a free allowance that resets
every day.

## Websites (manual assist only)

Browser automation is not implemented. Kling, PixVerse, Hailuo, Vidu, Pika,
Luma and SeaArt prohibit it in terms read on 2026-10-28. Google Flow's terms
also restrict automated access.

| Site | Daily video, low–high | Allowance page confirmed | Watermark on free |
| --- | --- | --- | --- |
| Google Flow | 40–70s | Yes | Unknown |
| PixVerse | 4–6s | Yes | Yes |
| Vidu | See the app | Yes | See the app |
| SeaArt | See the app | Yes | See the app |
| Pika | See the app | No | See the app |
| Kling | See the app | Yes | See the app |
| Hailuo | See the app | Yes | See the app |
| Luma | See the app | Yes | See the app |
| Krea | See the app | Yes | See the app |
| Higgsfield | See the app | Yes | See the app |

The app stores the exact quoted allowance, source URL and date on each site.
Daily Capacity adds them up and shows the gap against 20 minutes. Per-site
seconds that this file does not repeat are on that screen so a stale copy
here cannot disagree with the code.

Signup URLs are on the Manual Assist page. API signup URLs are on the
Providers page and in `.env.example`.

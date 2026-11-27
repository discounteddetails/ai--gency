# Pixel: Graphic Design Agent for Discounted Details

| | |
|---|---|
| **Role** | Designs offer posters and video thumbnails on request |
| **Status** | V1. The owner picks every design; nothing is posted without Nova's approval card |
| **Runs in** | The same Railway service and Telegram bot as Dex and Nova |
| **Model** | Claude Sonnet 5.5 (`PIXEL_MODEL`) |
| **Date** | 2026-11-27 |

## Brand (owner-confirmed)
- Colors: blues, black and white. Clean and modern.
- Fonts (bundled, OFL): Anton (display), Montserrat (headings), Inter (body).
- Logo: coming soon. The owner sends it as a file with the caption `/logo`, and it goes on every design after that. Until then, Pixel uses a "DISCOUNTED DETAILS" wordmark.

## Workflow
1. **Request in Telegram:**
   - `/design <what you need>` with no photos, or
   - photos or a video with `/design ...` as the caption. Albums are grouped. Videos become 2–3 frames to choose from.
2. **Two options:** Claude writes both as HTML/CSS for each size. Sizes:
   - Posters: feed 1080×1350 and story 1080×1920.
   - Thumbnails: YouTube 1280×720 and Reel cover 1080×1920.
3. **Previews:** the designs are rendered in headless Chromium and sent as an album with **✅ Use A / ✅ Use B / ✏️ Change… / 🔁 New ideas / ❌ Cancel**.
4. **Change…:** the owner types what to change. Pixel returns the edited design plus a fresh alternative.
5. **Use X:** the full-size files are sent as documents, with **📤 Post with Nova**. That creates a Nova offer post:
   - the feed image goes to the feeds,
   - the story image goes to Stories,
   - and it shows the usual approval card.

## Hard rules
| Rule | Where it's enforced |
|---|---|
| The renderer can't reach the network: JavaScript is off and every request outside the render folder is blocked | `app/pixel/render.py` (`page.route` guard, `java_script_enabled=False`) |
| Scripts, iframes, links, event handlers and `@import` are stripped from the HTML | `render._clean` |
| Only facts from the owner's request: no invented prices or deals, never "perfect", no addresses or names | Designer brief (`app/pixel/designer.py`), plus Nova's caption check when posting |
| Nothing posts without the owner tapping ✅ on Nova's card | `Pixel._to_nova` makes a *pending* Nova post |

## Limits
- Pixel designs with the owner's real photos, type and shapes. It doesn't generate photos.
- Telegram's 20 MB bot limit applies to videos. For long videos, send a screenshot instead.
